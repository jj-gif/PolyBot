"""
bot.py - Polymarket Discord Bot (multi-user edition)

Registration: users connect with their wallet ADDRESS (not private key).
Deposits detected automatically on-chain via wallet_monitor.py.
Virtual balances tracked per user - bot wallet holds all real funds.

Commands:
  /connect     - register wallet address via DM
  /disconnect  - remove registration (must withdraw first)
  /me          - show wallet + balance summary
  /deposit     - get the bot wallet address to send USDC to
  /withdraw    - send funds back to your wallet (2% fee on profits)
  /mybalance   - full balance breakdown + transaction history
  /search      - find Polymarket markets
  /run         - set up auto-trade via popup form
  /watch       - add market to watchlist
  /watches     - list your watches
  /remove      - cancel a watch
  /positions   - open positions + live P&L
  /balance     - check bot wallet USDC/MATIC on-chain
  /btcrun      - start BTC 5m auto-trader
  /btcstop     - stop BTC auto-trader
  /btcstatus   - check BTC session
  /admin_users - list all users (admin only)
  /admin_remove- force-remove user (admin only)
  /status      - bot health check
"""
import os
import asyncio
import logging
from dotenv import load_dotenv

# Load .env FIRST before any other imports so env vars are available
load_dotenv()

# Apply CLOB proxy immediately — must patch httpx before py_clob_client imports it
_clob_proxy = os.getenv("CLOB_PROXY", "").strip()
if _clob_proxy:
    os.environ["HTTPS_PROXY"] = _clob_proxy
    os.environ["HTTP_PROXY"]  = _clob_proxy
    os.environ["ALL_PROXY"]   = _clob_proxy

    # httpx does NOT read env vars automatically — must patch the class directly
    try:
        import httpx

        _orig_client_init = httpx.Client.__init__
        def _patched_client_init(self, *args, **kwargs):
            if "proxies" not in kwargs and "proxy" not in kwargs and "mounts" not in kwargs:
                kwargs["proxy"] = _clob_proxy
            _orig_client_init(self, *args, **kwargs)
        httpx.Client.__init__ = _patched_client_init

        _orig_async_init = httpx.AsyncClient.__init__
        def _patched_async_init(self, *args, **kwargs):
            if "proxies" not in kwargs and "proxy" not in kwargs and "mounts" not in kwargs:
                kwargs["proxy"] = _clob_proxy
            _orig_async_init(self, *args, **kwargs)
        httpx.AsyncClient.__init__ = _patched_async_init

        print(f"[startup] CLOB proxy set + httpx patched: {_clob_proxy[:50]}")
    except Exception as e:
        print(f"[startup] WARNING: Could not patch httpx: {e}")
else:
    print("[startup] No CLOB_PROXY set — trading will use direct connection")

import discord
from discord.ext import commands
from discord import app_commands
from web3 import Web3

import database as db
import polymarket_client as pm
import trader
import btc_trader
import btc_market
import balance as bal
import wallet_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TRADER_ROLE_NAME = os.getenv("TRADER_ROLE_NAME", "Trader")
BOT_WALLET       = os.getenv("BOT_WALLET_ADDRESS", "").strip()

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# Role check helpers

def has_trader_role(member: discord.Member) -> bool:
    return any(r.name == TRADER_ROLE_NAME for r in member.roles)


def require_trader_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not has_trader_role(interaction.user):
            await interaction.response.send_message(
                f"You need the **{TRADER_ROLE_NAME}** role to use this bot.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def require_registered():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not db.get_user(str(interaction.user.id)):
            await interaction.response.send_message(
                "You have not connected a wallet yet. Run `/connect` first.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def require_funds():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = db.get_user(str(interaction.user.id))
        if not user or user["virtual_balance"] < 1.0:
            await interaction.response.send_message(
                "Insufficient balance. Use `/deposit` to add funds first.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# on_ready

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    db.init_db()

    channel_id    = int(os.getenv("ALERT_CHANNEL_ID", 0))
    reports_id    = int(os.getenv("REPORTS_CHANNEL_ID", 0))

    if channel_id:
        ch = bot.get_channel(channel_id)
        trader.alert_channel         = ch
        btc_trader.alert_channel     = ch
        wallet_monitor.alert_channel = ch
        if ch:
            log.info(f"Alert channel: #{ch.name}")

    if reports_id:
        rch = bot.get_channel(reports_id)
        trader.reports_channel     = rch
        btc_trader.reports_channel = rch
        if rch:
            log.info(f"Reports channel: #{rch.name}")
    else:
        # Fall back to alert channel if no separate reports channel set
        trader.reports_channel     = trader.alert_channel
        btc_trader.reports_channel = btc_trader.alert_channel

    bot.loop.create_task(trader.monitor_loop())
    bot.loop.create_task(btc_trader.monitor_loop())
    bot.loop.create_task(_wallet_board_loop())
    await tree.sync()
    log.info("Ready")


# DM listener - collects wallet ADDRESS during registration

_pending_registration: dict[int, bool] = {}


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    # ── Wallet change flow ────────────────────────────────────────────────────
    if message.author.id in _pending_wallet_change:
        raw = message.content.strip()
        _pending_wallet_change.pop(message.author.id, None)

        if not (raw.startswith("0x") and len(raw) == 42):
            await message.author.send(
                "That doesn't look like a valid address (must start with `0x`, 42 chars).\n"
                "Run `/changewallet` to try again."
            )
            return

        try:
            address = Web3.to_checksum_address(raw)
        except Exception:
            await message.author.send("Invalid address format. Run `/changewallet` to try again.")
            return

        success = db.update_wallet(str(message.author.id), address)
        if not success:
            await message.author.send("❌ That address is already registered to another user.")
            return

        user = db.get_user(str(message.author.id))
        await message.author.send(
            f"✅ **Wallet updated!**\n\n"
            f"New address: `{address}`\n"
            f"Balance unchanged: `${user['virtual_balance']:.2f}`\n\n"
            f"Future deposits should now be sent from this address."
        )
        log.info(f"Wallet updated: {message.author} -> {address}")
        return

    # ── Registration flow ─────────────────────────────────────────────────────
    if message.author.id not in _pending_registration:
        return

    raw = message.content.strip()
    _pending_registration.pop(message.author.id, None)

    if not (raw.startswith("0x") and len(raw) == 42):
        await message.author.send(
            "That does not look like a valid wallet address.\n"
            "It should start with `0x` and be 42 characters long.\n"
            "Example: `0xAbCd...1234`\n\n"
            "Run `/connect` in the server to try again."
        )
        return

    try:
        address = Web3.to_checksum_address(raw)
    except Exception:
        await message.author.send("Invalid address format. Run `/connect` to try again.")
        return

    existing = db.get_user_by_wallet(address)
    if existing and existing["discord_id"] != str(message.author.id):
        await message.author.send("That wallet address is already registered to another user.")
        return

    db.register_user(
        discord_id     = str(message.author.id),
        discord_name   = str(message.author),
        wallet_address = address,
    )

    await message.author.send(
        f"Wallet registered!\n\n"
        f"Your address: `{address}`\n\n"
        f"To add funds, run `/deposit` in the server. The bot will detect your "
        f"USDC deposit automatically within ~30 seconds.\n\n"
        f"Minimum deposit: **$3.00 USDC** on Polygon network."
    )
    log.info(f"User registered: {message.author} -> {address}")
    await bot.process_commands(message)


# /connect

@tree.command(name="connect", description="Register your Polygon wallet address with the bot")
@require_trader_role()
async def cmd_connect(interaction: discord.Interaction):
    user = db.get_user(str(interaction.user.id))
    if user:
        await interaction.response.send_message(
            f"You already have a wallet connected: `{user['wallet_address']}`.\n"
            "Run `/disconnect` first if you want to change it.",
            ephemeral=True
        )
        return
    try:
        await interaction.user.send(
            "Connect Your Wallet\n\n"
            "Reply to this DM with your **Polygon wallet address**.\n\n"
            "This is the address you will be sending USDC FROM - "
            "the bot uses it to detect your deposits automatically.\n\n"
            "Where to find it:\n"
            "- MetaMask: click your account name at the top, copy address\n"
            "- Coinbase Wallet: tap your profile, copy address\n\n"
            "It looks like: `0xAbCd...1234` (42 characters starting with 0x)\n\n"
            "Do NOT send your private key - just the public address."
        )
        _pending_registration[interaction.user.id] = True
        await interaction.response.send_message(
            "Check your DMs - I sent you setup instructions.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I could not DM you. Enable DMs from server members in Privacy Settings, "
            "then try `/connect` again.",
            ephemeral=True
        )


# /changewallet

_pending_wallet_change: dict[int, bool] = {}

@tree.command(name="changewallet", description="Update your registered wallet address")
@require_trader_role()
@require_registered()
async def cmd_changewallet(interaction: discord.Interaction):
    user = db.get_user(str(interaction.user.id))
    try:
        await interaction.user.send(
            f"**Change Wallet Address**\n\n"
            f"Current address: `{user['wallet_address']}`\n"
            f"Current balance: `${user['virtual_balance']:.2f}`\n\n"
            f"Reply with your **new Polygon wallet address**.\n"
            f"Your balance will stay — only the deposit address changes.\n\n"
            f"Format: `0xAbCd...1234` (42 chars starting with 0x)"
        )
        _pending_wallet_change[interaction.user.id] = True
        await interaction.response.send_message(
            "Check your DMs — reply with your new wallet address.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I can't DM you. Enable DMs from server members and try again.", ephemeral=True
        )


# /disconnect

@tree.command(name="disconnect", description="Remove your connected wallet from the bot")
@require_trader_role()
async def cmd_disconnect(interaction: discord.Interaction):
    user = db.get_user(str(interaction.user.id))
    if not user:
        await interaction.response.send_message(
            "You do not have a wallet connected.", ephemeral=True
        )
        return
    if user["virtual_balance"] > 0:
        await interaction.response.send_message(
            f"You still have `${user['virtual_balance']:.2f}` in your balance.\n"
            f"Run `/withdraw {user['virtual_balance']:.2f}` first to get your funds back.",
            ephemeral=True
        )
        return
    db.deregister_user(str(interaction.user.id))
    await interaction.response.send_message(
        "Your wallet has been removed. Run `/connect` to reconnect.", ephemeral=True
    )


# /me

@tree.command(name="me", description="Show your connected wallet and balance")
@require_trader_role()
@require_registered()
async def cmd_me(interaction: discord.Interaction):
    user      = db.get_user(str(interaction.user.id))
    watches   = db.get_user_watches(str(interaction.user.id))
    positions = db.get_user_positions(str(interaction.user.id))
    active_w  = sum(1 for w in watches if w["status"] == "watching")
    profit    = max(0, user["virtual_balance"] - user["total_deposited"])
    await interaction.response.send_message(
        f"Your Profile\n"
        f"Wallet:     `{user['wallet_address']}`\n"
        f"Registered: `{user['registered_at']}`\n\n"
        f"Balance:    `${user['virtual_balance']:.2f}`\n"
        f"Deposited:  `${user['total_deposited']:.2f}`\n"
        f"Profit:     `${profit:.2f}`\n\n"
        f"Watches: `{active_w}` active | Positions: `{len(positions)}` open",
        ephemeral=True
    )


# /deposit

@tree.command(name="deposit", description="Get the bot wallet address to send USDC to")
@require_trader_role()
@require_registered()
async def cmd_deposit(interaction: discord.Interaction):
    user = db.get_user(str(interaction.user.id))
    if not BOT_WALLET:
        await interaction.response.send_message(
            "Bot wallet not configured. Contact admin.", ephemeral=True
        )
        return

    if wallet_monitor.is_watching(str(interaction.user.id)):
        await interaction.response.send_message(
            f"Already watching for your deposit — just send USDC from "
            f"`{user['wallet_address']}` to the address below and it will be detected automatically.\n"
            f"```{BOT_WALLET}```",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"💵 **Deposit USDC**\n\n"
        f"Send **USDC on Polygon** to:\n"
        f"```{BOT_WALLET}```\n"
        f"**Important:**\n"
        f"• Send from your registered address: `{user['wallet_address']}`\n"
        f"• Minimum deposit: **$3.00 USDC**\n"
        f"• Network: **Polygon only** (not Ethereum mainnet)\n\n"
        f"Your current balance: `${user['virtual_balance']:.2f}`\n\n"
        f"⏳ Watching for your deposit for the next **15 minutes**...",
        ephemeral=True
    )

    bot.loop.create_task(
        wallet_monitor.watch_for_deposit(str(interaction.user.id), interaction.followup)
    )


# /withdraw

@tree.command(name="withdraw", description="Withdraw USDC from your balance back to your wallet")
@require_trader_role()
@require_registered()
@app_commands.describe(amount="Amount in USDC to withdraw (e.g. 10.00)")
async def cmd_withdraw(interaction: discord.Interaction, amount: float):
    result = db.process_withdrawal(str(interaction.user.id), amount)
    if not result["ok"]:
        await interaction.response.send_message(f"{result['reason']}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    net = result["net_amount"]
    fee = result["fee"]
    user = db.get_user(str(interaction.user.id))

    try:
        from eth_account import Account
        rpc_urls = [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon.llamarpc.com",
        ]
        w3 = None
        for url in rpc_urls:
            try:
                _w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue

        if not w3:
            raise ConnectionError("Could not connect to Polygon")

        bot_key = os.getenv("BOT_WALLET_PRIVATE_KEY", "").strip()
        if not bot_key:
            raise ValueError("BOT_WALLET_PRIVATE_KEY not set in .env")

        USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        USDC_ABI = [{"inputs": [{"internalType": "address","name": "to","type": "address"},{"internalType": "uint256","name": "amount","type": "uint256"}],"name": "transfer","outputs": [{"internalType": "bool","name": "","type": "bool"}],"stateMutability": "nonpayable","type": "function"}]

        account    = Account.from_key(bot_key)
        contract   = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
        amount_raw = int(net * 1_000_000)
        nonce      = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.transfer(
            Web3.to_checksum_address(user["wallet_address"]), amount_raw
        ).build_transaction({
            "from": account.address, "nonce": nonce,
            "gasPrice": w3.eth.gas_price, "chainId": 137,
        })
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
        new_bal = db.get_user(str(interaction.user.id))["virtual_balance"]
        fee_line = f"\nFee (2% on profit): `${fee:.4f}`" if fee > 0 else ""
        await interaction.followup.send(
            f"Withdrawal sent!\n"
            f"Amount: `${net:.2f} USDC`{fee_line}\n"
            f"To: `{user['wallet_address']}`\n"
            f"Tx: `{tx_hash}`\n"
            f"Remaining balance: `${new_bal:.2f}`",
            ephemeral=True
        )
    except Exception as e:
        log.error(f"Withdrawal failed for {interaction.user}: {e}")
        db.credit_balance(str(interaction.user.id), amount, note="Withdrawal refund (tx failed)")
        await interaction.followup.send(
            f"Transfer failed: `{e}`\nYour balance has been refunded.", ephemeral=True
        )


# /mybalance

@tree.command(name="mybalance", description="Show your full balance and transaction history")
@require_trader_role()
@require_registered()
async def cmd_mybalance(interaction: discord.Interaction):
    user    = db.get_user(str(interaction.user.id))
    history = db.get_transaction_history(str(interaction.user.id), limit=5)
    profit  = max(0, user["virtual_balance"] - user["total_deposited"])
    emoji   = "up" if profit > 0 else ("down" if user["virtual_balance"] < user["total_deposited"] else "flat")
    icons   = {"deposit": "💵", "withdraw": "💸", "fee": "✂️", "trade_debit": "🔴", "trade_credit": "🟢"}
    hist_lines = [
        f"{icons.get(tx['type'],'•')} `{tx['type']:12}` `${tx['amount']:>8.2f}` {tx['note'] or ''}"
        for tx in history
    ] or ["No transactions yet"]
    await interaction.response.send_message(
        f"Your Balance\n\n"
        f"Available:  `${user['virtual_balance']:.2f}`\n"
        f"Deposited:  `${user['total_deposited']:.2f}`\n"
        f"Profit:     `${profit:.2f}`\n\n"
        f"Recent Transactions:\n" + "\n".join(hist_lines),
        ephemeral=True
    )

# ── /search — paginated results ───────────────────────────────────────────────

def _build_search_embed(markets: list, page: int, total_pages: int, query: str) -> discord.Embed:
    """Build one page of search results as a Discord embed."""
    embed = discord.Embed(
        title=f"🔍 Search: \"{query}\"",
        description=f"Page {page + 1} of {total_pages}  •  Use the buttons to scroll",
        color=0x6B46C1
    )
    for m in markets:
        token_lines = []
        for t in m["tokens"]:
            token_lines.append(
                f"**{t['outcome']}** — `{float(t['price'])*100:.1f}%`\n"
                f"```{t['token_id']}```"
            )
        embed.add_field(
            name=m["question"][:200],
            value="\n".join(token_lines) or "No tokens",
            inline=False
        )
    embed.set_footer(text="Copy a Token ID then use /run — expires in 5 minutes")
    return embed


class SearchPaginator(discord.ui.View):
    def __init__(self, markets: list, query: str, per_page: int = 3):
        super().__init__(timeout=300)  # 5 minutes
        self.markets    = markets
        self.query      = query
        self.per_page   = per_page
        self.page       = 0
        self.total_pages = max(1, (len(markets) + per_page - 1) // per_page)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label    = f"{self.page + 1} / {self.total_pages}"

    def current_embed(self) -> discord.Embed:
        start  = self.page * self.per_page
        chunk  = self.markets[start:start + self.per_page]
        return _build_search_embed(chunk, self.page, self.total_pages, self.query)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self):
        # Disable all buttons when expired
        for item in self.children:
            item.disabled = True


@tree.command(name="search", description="Search Polymarket for markets by keyword")
@require_trader_role()
@app_commands.describe(query="Keywords to search for (e.g. 'bitcoin', 'Trump', 'BTC')")
async def cmd_search(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    markets = await pm.search_markets(query, limit=50)

    if not markets:
        await interaction.followup.send(
            f"❌ No active markets found for **{query}**.\n"
            f"Try a different keyword — e.g. `BTC`, `Trump`, `election`."
        )
        return

    view  = SearchPaginator(markets, query, per_page=3)
    embed = view.current_embed()
    await interaction.followup.send(embed=embed, view=view)


# ── /run — Modal form for all thresholds ──────────────────────────────────────

class RunTradeModal(discord.ui.Modal, title="⚡ Set Up Auto-Trade"):
    """
    Popup form that collects every threshold for a new watch in one shot.
    Opens when the user runs /run <token_id> <outcome>.
    """

    buy_price = discord.ui.TextInput(
        label="Buy-in Price (0–1)",
        placeholder="e.g. 0.40  →  Buy when market price drops TO or BELOW this",
        required=True,
        max_length=10,
    )
    take_profit = discord.ui.TextInput(
        label="Take-Profit Price (0–1)  [optional]",
        placeholder="e.g. 0.75  →  Sell when price RISES to this. Leave blank to skip.",
        required=False,
        max_length=10,
    )
    stop_pct = discord.ui.TextInput(
        label="% Drop Stop-Loss  [optional]",
        placeholder="e.g. 10  →  Sell if price drops 10% from your avg buy. Leave blank to skip.",
        required=False,
        max_length=6,
    )
    stop_loss = discord.ui.TextInput(
        label="Absolute Floor Price (0–1)  [optional]",
        placeholder="e.g. 0.30  →  Sell if price falls BELOW this no matter what. Leave blank to skip.",
        required=False,
        max_length=10,
    )
    trade_size = discord.ui.TextInput(
        label="Trade Size (USD)",
        placeholder="e.g. 25  →  Spend this many USDC when the buy triggers. Default: 10",
        required=False,
        max_length=10,
        default="10",
    )

    def __init__(self, token_id: str, outcome: str, current_price: float):
        super().__init__()
        self.token_id      = token_id
        self.outcome       = outcome
        self.current_price = current_price

    async def on_submit(self, interaction: discord.Interaction):
        errors = []

        # ── Parse & validate each field ──────────────────────────────────────
        try:
            buy_p = float(self.buy_price.value.strip())
            if not (0 < buy_p <= 1):
                errors.append("• **Buy-in price** must be between 0 and 1 (e.g. `0.40`)")
        except ValueError:
            errors.append("• **Buy-in price** must be a number (e.g. `0.40`)")
            buy_p = None

        tp = None
        if self.take_profit.value.strip():
            try:
                tp = float(self.take_profit.value.strip())
                if not (0 < tp <= 1):
                    errors.append("• **Take-profit** must be between 0 and 1 (e.g. `0.75`)")
                    tp = None
            except ValueError:
                errors.append("• **Take-profit** must be a number or left blank")

        sp = None
        if self.stop_pct.value.strip():
            try:
                sp = float(self.stop_pct.value.strip())
                if not (0 < sp < 100):
                    errors.append("• **% Drop stop-loss** must be between 1 and 99 (e.g. `10`)")
                    sp = None
            except ValueError:
                errors.append("• **% Drop stop-loss** must be a number or left blank")

        sl = None
        if self.stop_loss.value.strip():
            try:
                sl = float(self.stop_loss.value.strip())
                if not (0 < sl <= 1):
                    errors.append("• **Absolute floor** must be between 0 and 1 (e.g. `0.30`)")
                    sl = None
            except ValueError:
                errors.append("• **Absolute floor** must be a number or left blank")

        size = 10.0
        if self.trade_size.value.strip():
            try:
                size = float(self.trade_size.value.strip())
                if size <= 0:
                    errors.append("• **Trade size** must be greater than 0")
                    size = 10.0
            except ValueError:
                errors.append("• **Trade size** must be a number (e.g. `25`)")

        # Cross-field sanity checks
        if buy_p and tp and tp <= buy_p:
            errors.append("• **Take-profit** should be higher than your buy-in price")
        if buy_p and sl and sl >= buy_p:
            errors.append("• **Absolute floor** should be lower than your buy-in price")

        if errors:
            await interaction.response.send_message(
                "❌ **Please fix the following:**\n" + "\n".join(errors),
                ephemeral=True
            )
            return

        if buy_p is None:
            await interaction.response.send_message("❌ Buy-in price is required.", ephemeral=True)
            return

        # ── Save watch ────────────────────────────────────────────────────────
        watch_id = db.add_watch(
            discord_id  = str(interaction.user.id),
            token_id    = self.token_id,
            question    = f"Token {self.token_id[:16]}…",
            outcome     = self.outcome,
            buy_price   = buy_p,
            stop_loss   = sl,
            stop_pct    = sp,
            take_profit = tp,
            trade_size  = size,
        )

        # ── Build summary ─────────────────────────────────────────────────────
        above_below = "🔴 Already at/below trigger" if self.current_price <= buy_p else "🟡 Waiting…"

        tp_line  = f"🎯 Take-profit:      `{tp:.4f}` (sell when price hits this)" if tp else "🎯 Take-profit:      off"
        sp_line  = f"📉 % Drop stop-loss: `{sp}%` (sell if price drops {sp}% from avg buy)" if sp else "📉 % Drop stop-loss: off"
        sl_line  = f"🛑 Absolute floor:   `{sl:.4f}` (hard sell floor)" if sl else "🛑 Absolute floor:   off"

        await interaction.response.send_message(
            f"✅ **Auto-trade armed!** (Watch ID `{watch_id}`)\n"
            f"```\n"
            f"Token:    {self.token_id[:32]}…\n"
            f"Outcome:  {self.outcome}\n"
            f"```\n"
            f"▶ **Buy trigger:**      `{buy_p:.4f}` — {above_below}\n"
            f"   Current price:    `{self.current_price:.4f}`\n\n"
            f"{tp_line}\n"
            f"{sp_line}\n"
            f"{sl_line}\n\n"
            f"💵 **Trade size:** `${size:.2f} USDC` per buy\n\n"
            f"The bot will monitor this every `{os.getenv('POLL_INTERVAL', 30)}s` "
            f"and alert you in <#{os.getenv('ALERT_CHANNEL_ID', '?')}> when it fires.\n"
            f"Use `/watches` to see all your watches or `/remove {watch_id}` to cancel.",
            ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error(f"RunTradeModal error: {error}")
        await interaction.response.send_message(
            "❌ Something went wrong. Please try `/run` again.", ephemeral=True
        )


@tree.command(
    name="run",
    description="Set up an auto-trade with all thresholds via a form popup"
)
@require_trader_role()
@require_registered()
@app_commands.describe(
    token_id = "Token ID from /search",
    outcome  = "Yes or No",
)
async def cmd_run(
    interaction: discord.Interaction,
    token_id:    str,
    outcome:     str,
):
    # Fetch live price before showing the modal so we can display it in the form
    current_price = await pm.get_token_price(token_id)
    if current_price is None:
        await interaction.response.send_message(
            "⚠️ Couldn't fetch the current price for that token — double-check the token ID.",
            ephemeral=True
        )
        return

    modal = RunTradeModal(
        token_id      = token_id,
        outcome       = outcome,
        current_price = current_price,
    )
    await interaction.response.send_modal(modal)


# ── /watch ────────────────────────────────────────────────────────────────────

@tree.command(name="watch", description="Add a market to your automated watchlist")
@require_trader_role()
@require_registered()
@app_commands.describe(
    token_id    = "Token ID from /search",
    outcome     = "Yes or No",
    buy_price   = "Buy when price ≤ this (0–1)",
    stop_pct    = "Sell if price drops X% from avg buy (default 10). Set 0 to disable.",
    take_profit = "Sell when price ≥ this (0–1). Set 0 to disable.",
    stop_loss   = "Absolute sell floor (0–1). Set 0 to disable.",
    trade_size  = "USD to spend per buy (default 10)",
)
async def cmd_watch(
    interaction: discord.Interaction,
    token_id:    str,
    outcome:     str,
    buy_price:   float,
    stop_pct:    float = 10.0,
    take_profit: float = 0.0,
    stop_loss:   float = 0.0,
    trade_size:  float = 10.0,
):
    if not (0 < buy_price <= 1):
        await interaction.response.send_message("❌ buy_price must be between 0 and 1.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    current_price = await pm.get_token_price(token_id)
    if current_price is None:
        await interaction.followup.send("⚠️ Couldn't fetch price — double-check the token ID.", ephemeral=True)
        return

    watch_id = db.add_watch(
        discord_id  = str(interaction.user.id),
        token_id    = token_id,
        question    = f"Token {token_id[:16]}…",
        outcome     = outcome,
        buy_price   = buy_price,
        stop_loss   = stop_loss   if stop_loss   > 0 else None,
        stop_pct    = stop_pct    if stop_pct    > 0 else None,
        take_profit = take_profit if take_profit > 0 else None,
        trade_size  = trade_size,
    )

    await interaction.followup.send(
        f"✅ **Watch added** (ID `{watch_id}`)\n"
        f"Token: `{token_id[:24]}…` | Outcome: **{outcome}**\n"
        f"Current price: `{current_price:.4f}`\n"
        f"▶ Buy trigger:  ≤ `{buy_price:.4f}`\n"
        f"🛑 Stop-loss:   `{stop_loss or 'off'}` | Stop %: `{stop_pct or 'off'}%`\n"
        f"🎯 Take-profit: `{take_profit or 'off'}`\n"
        f"💵 Trade size:  `${trade_size:.2f}`",
        ephemeral=True
    )


# ── /watches ──────────────────────────────────────────────────────────────────

@tree.command(name="watches", description="List your market watches")
@require_trader_role()
@require_registered()
async def cmd_watches(interaction: discord.Interaction):
    rows = db.get_user_watches(str(interaction.user.id))
    if not rows:
        await interaction.response.send_message("No watches yet. Use `/watch` to add one.", ephemeral=True)
        return

    lines = ["📋 **Your Watches:**\n"]
    for w in rows:
        emoji = {"watching": "👀", "filled": "✅", "closed": "❌"}.get(w["status"], "❓")
        lines.append(
            f"{emoji} ID `{w['id']}` — {w['question']}\n"
            f"   **{w['outcome']}** | Buy ≤ `{w['buy_price']:.4f}` | "
            f"TP: `{w['take_profit'] or 'off'}` | SL: `{w['stop_loss'] or 'off'}` | "
            f"Stop%: `{w['stop_pct'] or 'off'}%` | `${w['trade_size']:.2f}`\n"
        )
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


# ── /remove ───────────────────────────────────────────────────────────────────

@tree.command(name="remove", description="Cancel one of your watches")
@require_trader_role()
@require_registered()
@app_commands.describe(watch_id="The watch ID from /watches")
async def cmd_remove(interaction: discord.Interaction, watch_id: int):
    db.remove_watch(str(interaction.user.id), watch_id)
    await interaction.response.send_message(f"🗑️ Watch `{watch_id}` removed.", ephemeral=True)


# ── /positions ────────────────────────────────────────────────────────────────

@tree.command(name="positions", description="Show your open positions with live P&L")
@require_trader_role()
@require_registered()
async def cmd_positions(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    positions = db.get_user_positions(str(interaction.user.id))

    if not positions:
        await interaction.followup.send("No open positions right now.", ephemeral=True)
        return

    lines = ["📊 **Your Open Positions:**\n"]
    for pos in positions:
        price = await pm.get_token_price(pos["token_id"])
        if price:
            pnl     = (price - pos["avg_price"]) * pos["shares"]
            pnl_pct = ((price / pos["avg_price"]) - 1) * 100
            pnl_str = f"`{'%+.2f' % pnl} USDC` (`{'%+.1f' % pnl_pct}%`)"
        else:
            pnl_str = "`N/A`"

        lines.append(
            f"🔷 ID `{pos['id']}` — {pos['question']}\n"
            f"   **{pos['outcome']}** | Avg buy: `{pos['avg_price']:.4f}` | "
            f"Shares: `{pos['shares']:.2f}` | PnL: {pnl_str}\n"
        )
    await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)


# ── /status ───────────────────────────────────────────────────────────────────

@tree.command(name="status", description="Bot health and config")
async def cmd_status(interaction: discord.Interaction):
    users     = db.get_all_users()
    watches   = db.get_active_watches()
    positions = db.get_open_positions()
    channel   = f"#{trader.alert_channel.name}" if trader.alert_channel else "⚠️ Not set"
    poll      = os.getenv("POLL_INTERVAL", 30)

    await interaction.response.send_message(
        f"**Polymarket Bot Status**\n"
        f"Alert channel:    {channel}\n"
        f"Poll interval:    `{poll}s`\n"
        f"Registered users: `{len(users)}`\n"
        f"Active watches:   `{len(watches)}`\n"
        f"Open positions:   `{len(positions)}`\n"
        f"Trader role:      `{TRADER_ROLE_NAME}`",
        ephemeral=True
    )


# ── Admin: /admin_users ───────────────────────────────────────────────────────

@tree.command(name="admin_users", description="[Admin] List all registered users")
@app_commands.default_permissions(manage_guild=True)
async def cmd_admin_users(interaction: discord.Interaction):
    users = db.get_all_users()
    if not users:
        await interaction.response.send_message("No registered users.", ephemeral=True)
        return

    lines = [f"👥 **Registered Users ({len(users)}):**\n"]
    for u in users:
        lines.append(
            f"• <@{u['discord_id']}> (`{u['discord_name']}`)\n"
            f"  Wallet: `{u['wallet_address']}` | Since: `{u['registered_at']}`\n"
        )
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


# ── Admin: /admin_credit ──────────────────────────────────────────────────────

@tree.command(name="admin_credit", description="[Admin] Manually credit funds to a user's balance")
@app_commands.default_permissions(manage_guild=True)
async def cmd_admin_credit(interaction: discord.Interaction,
                           user: discord.Member,
                           amount: float,
                           reason: str = "Manual admin credit"):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
        return

    db_user = db.get_user(str(user.id))
    if not db_user:
        await interaction.response.send_message(
            f"❌ {user.mention} is not registered.", ephemeral=True
        )
        return

    old_balance = db_user["virtual_balance"]
    db.credit_balance(
        discord_id = str(user.id),
        amount     = amount,
        tx_hash    = f"admin-credit-{interaction.user.id}-{int(__import__('time').time())}",
        note       = reason
    )
    new_user    = db.get_user(str(user.id))
    new_balance = new_user["virtual_balance"] if new_user else old_balance + amount

    await interaction.response.send_message(
        f"✅ **Credit applied**\n"
        f"User: {user.mention}\n"
        f"Amount: `+${amount:.2f}`\n"
        f"Reason: `{reason}`\n"
        f"Balance: `${old_balance:.2f}` → `${new_balance:.2f}`",
        ephemeral=True
    )
    log.info(f"Admin credit: {interaction.user} gave ${amount:.2f} to {user} — {reason}")

    # Try to notify the user
    try:
        await user.send(
            f"💵 **You've been credited `${amount:.2f} USDC`** by an admin.\n"
            f"Reason: `{reason}`\n"
            f"New balance: `${new_balance:.2f}`"
        )
    except Exception:
        pass




@tree.command(name="admin_remove", description="[Admin] Force-remove a user's wallet registration")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(user="The Discord user to remove")
async def cmd_admin_remove(interaction: discord.Interaction, user: discord.Member):
    db.deregister_user(str(user.id))
    trader.invalidate_clob_cache(str(user.id))
    await interaction.response.send_message(
        f"✅ Removed wallet registration for {user.mention}.",
        ephemeral=True
    )


# ── /balance ──────────────────────────────────────────────────────────────────

@tree.command(name="balance", description="[Admin only] Check the true bot wallet USDC and MATIC balance")
@app_commands.default_permissions(manage_guild=True)
async def cmd_balance(interaction: discord.Interaction):
    if not BOT_WALLET:
        await interaction.response.send_message(
            "BOT_WALLET_ADDRESS not set in .env", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        balances = await interaction.client.loop.run_in_executor(
            None, bal.get_balances, BOT_WALLET
        )

        usdc  = balances["usdc"]
        matic = balances["matic"]

        # Tally all user virtual balances for comparison
        all_users    = db.get_all_users()
        total_virtual = sum(u["virtual_balance"] for u in all_users)

        matic_warning = ""
        if matic < 0.1:
            matic_warning = (
                "\n⚠️ Low MATIC — bot needs MATIC for gas on withdrawals (0.5+ recommended)"
            )

        await interaction.followup.send(
            f"🏦 **Bot Wallet Balance** _(admin only)_\n"
            f"Address: `{BOT_WALLET}`\n\n"
            f"USDC (real):    `${usdc:,.2f}`\n"
            f"MATIC:          `{matic:.4f}`\n\n"
            f"User balances:  `${total_virtual:,.2f}` across `{len(all_users)}` users"
            f"{matic_warning}",
            ephemeral=True
        )

    except Exception as e:
        log.error(f"Balance check failed: {e}")
        await interaction.followup.send(
            "Couldn't fetch balance — Polygon RPC may be slow. Try again in a moment.",
            ephemeral=True
        )


# ── /setwallet — live balance board ──────────────────────────────────────────

# Stores the live wallet message so we can keep editing it { channel_id: message }
_wallet_board: dict[int, discord.Message] = {}


async def _build_wallet_embed() -> discord.Embed:
    """Build the live wallet balance embed."""
    import datetime
    all_users     = db.get_all_users()
    total_virtual = sum(u["virtual_balance"] for u in all_users)
    user_count    = len(all_users)

    embed = discord.Embed(
        title="🏦 Bot Wallet — Live Balance",
        color=0x00C853,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    # Try to fetch real on-chain balance
    try:
        loop     = asyncio.get_event_loop()
        balances = await loop.run_in_executor(None, bal.get_balances, BOT_WALLET)
        usdc     = balances["usdc"]
        matic    = balances["matic"]
        embed.add_field(name="USDC (on-chain)", value=f"`${usdc:,.2f}`",  inline=True)
        embed.add_field(name="MATIC",            value=f"`{matic:.4f}`",  inline=True)
        embed.add_field(name="\u200b",            value="\u200b",         inline=True)

        reserve = usdc - total_virtual
        embed.add_field(
            name="User balances",
            value=f"`${total_virtual:,.2f}` across `{user_count}` users",
            inline=True
        )
        embed.add_field(
            name="Reserve",
            value=f"`${reserve:,.2f}`",
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        if matic < 0.5:
            embed.add_field(
                name="⚠️ Low MATIC",
                value="Bot needs MATIC for withdrawal gas fees — top up soon",
                inline=False
            )
    except Exception as e:
        embed.add_field(name="⚠️ RPC Error", value=str(e)[:100], inline=False)
        embed.add_field(
            name="User balances (virtual)",
            value=f"`${total_virtual:,.2f}` across `{user_count}` users",
            inline=False
        )

    embed.add_field(
        name="Wallet address",
        value=f"`{BOT_WALLET}`" if BOT_WALLET else "_not configured_",
        inline=False
    )
    embed.set_footer(text="Updates every 5 minutes")
    return embed


async def _wallet_board_loop():
    """Edit the wallet board message every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        for channel_id, message in list(_wallet_board.items()):
            try:
                embed = await _build_wallet_embed()
                await message.edit(embed=embed)
            except discord.NotFound:
                _wallet_board.pop(channel_id, None)
            except Exception as e:
                log.warning(f"Wallet board update failed: {e}")


@tree.command(name="setwallet", description="[Admin] Post a live-updating bot wallet balance in this channel")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setwallet(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    embed   = await _build_wallet_embed()
    message = await interaction.channel.send(embed=embed)
    _wallet_board[interaction.channel_id] = message

    await interaction.followup.send(
        "✅ Wallet board posted — it will update every 5 minutes automatically.",
        ephemeral=True
    )


# ── /btcrun — BTC Up/Down 5m bot with full threshold modal ───────────────────

class BtcRunModal(discord.ui.Modal, title="⚡ BTC Up/Down 5m — Configure Bot"):

    buy_threshold = discord.ui.TextInput(
        label="Buy Threshold %",
        placeholder="e.g. 60  — buy whichever side (Up or Down) hits this %",
        required=True,
        max_length=6,
    )

    sizing = discord.ui.TextInput(
        label="Layer Size  /  Add every X%  /  Max layers",
        placeholder="e.g. 10 / 2 / 5   or just   10   for a single layer",
        required=True,
        max_length=20,
    )

    hard_stop_field = discord.ui.TextInput(
        label="Trailing Stop %",
        placeholder="e.g. 5  — sell everything if price drops 5% from its peak",
        required=True,
        max_length=5,
    )

    mode_and_savings = discord.ui.TextInput(
        label="Mode  |  dry $amount  |  savings wallet",
        placeholder="auto  or  10 rounds  |  dry 500  |  0xWallet (all optional)",
        required=False,
        max_length=100,
        default="auto",
    )

    async def on_submit(self, interaction: discord.Interaction):
        errors = []

        # ── Parse buy threshold ───────────────────────────────────────────────
        try:
            bt = float(self.buy_threshold.value.strip().replace("%", ""))
            if not (50 < bt <= 100):
                errors.append("• **Buy threshold** must be between 50 and 100 (e.g. `60`)")
                bt = None
            else:
                bt = bt / 100
        except ValueError:
            errors.append("• **Buy threshold** must be a number like `60`")
            bt = None

        # ── Parse sizing (layer size / add% / max layers) ─────────────────────
        sizing_raw = self.sizing.value.strip()
        parts      = [p.strip() for p in sizing_raw.split("/")]
        add_pct    = 0.02   # default: add every 2%
        max_layers = 1      # default: single layer

        try:
            base_size = float(parts[0])
            if base_size <= 0:
                errors.append("• **Layer size** must be greater than 0")
                base_size = None
        except ValueError:
            errors.append("• **Layer size** must be a number like `10`")
            base_size = None

        if len(parts) >= 2:
            try:
                add_pct = float(parts[1]) / 100
                if not (0 < add_pct < 1):
                    errors.append("• **Add every X%** must be between 0.1 and 99")
                    add_pct = 0.02
            except ValueError:
                errors.append("• **Add every X%** must be a number like `2`")

        if len(parts) >= 3:
            try:
                max_layers = int(parts[2])
                if max_layers < 1:
                    errors.append("• **Max layers** must be at least 1")
                    max_layers = 1
            except ValueError:
                errors.append("• **Max layers** must be a whole number like `5`")

        # ── Parse trailing stop ───────────────────────────────────────────────
        try:
            hard_stop_pct = float(self.hard_stop_field.value.strip().replace("%", ""))
            if not (0 < hard_stop_pct < 100):
                errors.append("• **Trailing stop** must be between 0.1 and 99 (e.g. `5`)")
                hard_stop_pct = None
            else:
                hard_stop_pct = hard_stop_pct / 100
        except ValueError:
            errors.append("• **Trailing stop** must be a number like `5`")
            hard_stop_pct = None

        # ── Parse mode, dry run & savings ─────────────────────────────────────
        mode_raw   = (self.mode_and_savings.value or "auto").strip()
        mode_parts = [p.strip() for p in mode_raw.split("|")]

        full_auto  = True
        max_rounds = 9999
        savings    = ""
        dry_run    = False

        wallet_part = next((p for p in mode_parts if p.startswith("0x") and len(p) >= 40), None)
        if wallet_part:
            savings    = wallet_part
            mode_parts = [p for p in mode_parts if p != wallet_part]

        # Extract dry run flag and optional starting balance
        # e.g. "dry" = $100 default, "dry 500" = $500
        dry_part = next((p for p in mode_parts if p.lower().strip().startswith("dry") or p.lower().strip() == "test"), None)
        if dry_part:
            dry_run    = True
            mode_parts = [p for p in mode_parts if p != dry_part]
            # Check for amount after "dry", e.g. "dry 500"
            parts_of_dry = dry_part.strip().split()
            if len(parts_of_dry) >= 2:
                try:
                    dry_run_balance = float(parts_of_dry[1])
                except ValueError:
                    dry_run_balance = 100.0
            else:
                dry_run_balance = 100.0
        else:
            dry_run_balance = 100.0

        mode_str = mode_parts[0].lower().strip() if mode_parts else "auto"
        if mode_str == "auto":
            full_auto  = True
            max_rounds = 9999
        else:
            try:
                max_rounds = int(mode_str.split()[0])
                full_auto  = False
                if max_rounds < 1:
                    errors.append("• **Rounds** must be at least 1")
                    max_rounds = 10
            except ValueError:
                errors.append("• **Mode** must be `auto` or a number like `10 rounds`")

        if errors:
            await interaction.response.send_message(
                "❌ **Please fix the following:**\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        if bt is None or base_size is None or hard_stop_pct is None:
            await interaction.response.send_message("❌ Required fields missing.", ephemeral=True)
            return

        # ── Create and start session ──────────────────────────────────────────
        session = btc_trader.BtcSession(
            discord_id      = str(interaction.user.id),
            buy_threshold   = bt,
            hard_stop_pct   = hard_stop_pct,
            base_size       = base_size,
            add_pct         = add_pct,
            max_layers      = max_layers,
            full_auto       = full_auto,
            max_rounds      = max_rounds,
            savings_wallet  = savings,
            dry_run         = dry_run,
            dry_run_balance = dry_run_balance,
        )
        btc_trader.start_session(session)

        mode_line    = "♾️ Full auto" if full_auto else f"🔢 Semi-auto: `{max_rounds}` rounds"
        savings_line = f"🏦 Savings: `{savings[:14]}…` (2% per win)" if savings else "🏦 Savings: off"
        dry_line     = (f"🧪 **DRY RUN** — simulated `${dry_run_balance:.2f}` starting balance, output → reports channel"
                        if dry_run else "💸 **LIVE** — real USDC")
        layer_line   = (f"📊 `${base_size:.2f}` per layer | add every `{add_pct*100:.1f}%` rise | max `{max_layers}` layers"
                        if max_layers > 1 else f"📊 `${base_size:.2f}` single layer")

        await interaction.response.send_message(
            f"✅ **BTC bot armed!** <@{interaction.user.id}>\n\n"
            f"Watching **both Up and Down**\n"
            f"Buy at: `{bt*100:.1f}%` — whichever side hits first\n"
            f"Trailing stop: `{hard_stop_pct*100:.1f}%` drop from peak → sell everything\n"
            f"Threshold floor: price drops back to `{bt*100:.1f}%` → sell everything\n\n"
            f"{layer_line}\n"
            f"{mode_line} | {savings_line}\n"
            f"{dry_line}\n\n"
            f"Watching live BTC 5m round. Run `/btcstop` to stop.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error(f"BtcRunModal error: {error}")
        await interaction.response.send_message("❌ Something went wrong. Try `/btcrun` again.", ephemeral=True)


@tree.command(name="btcrun", description="Start the BTC Up/Down 5m auto-trader")
@require_trader_role()
@require_registered()
async def cmd_btcrun(interaction: discord.Interaction):
    existing = btc_trader.get_session(str(interaction.user.id))
    if existing and not existing.stopped:
        await interaction.response.send_message(
            "⚠️ You already have a BTC session running. Use `/btcstop` to stop it first.",
            ephemeral=True
        )
        return
    await interaction.response.send_modal(BtcRunModal())


@tree.command(name="btcstop", description="Stop your BTC Up/Down auto-trader")
@require_trader_role()
async def cmd_btcstop(interaction: discord.Interaction):
    session = btc_trader.get_session(str(interaction.user.id))
    if not session:
        await interaction.response.send_message("You don't have an active BTC session.", ephemeral=True)
        return
    btc_trader.stop_session(str(interaction.user.id))
    await interaction.response.send_message(
        f"🛑 BTC auto-trader stopped. Rounds completed: `{session.rounds_done}`",
        ephemeral=True
    )


@tree.command(name="btcstatus", description="Check your current BTC session status")
@require_trader_role()
async def cmd_btcstatus(interaction: discord.Interaction):
    session = btc_trader.get_session(str(interaction.user.id))
    if not session or session.stopped:
        await interaction.response.send_message("No active BTC session.", ephemeral=True)
        return

    mode = "Full auto" if session.full_auto else f"Semi-auto ({session.rounds_done}/{session.max_rounds} rounds)"
    dry  = " 🧪 DRY RUN" if session.dry_run else ""

    if session.layers:
        layer_lines = "\n".join(
            f"  Layer {i+1}: entry `{btc_market_pct(l.entry_price):.2f}%` | "
            f"`{l.shares:.4f}` shares | `${l.size_usdc:.2f}`"
            for i, l in enumerate(session.layers)
        )
    else:
        layer_lines = "  None (waiting for entry)"

    user = db.get_user(str(interaction.user.id))
    bal  = user["virtual_balance"] if user else 0

    await interaction.response.send_message(
        f"**Your BTC Session**{dry}\n"
        f"Holding:        **{session.active_side if session.active_side else 'None — watching both'}**\n"
        f"Buy threshold:  `{session.buy_threshold*100:.1f}%`\n"
        f"Trailing stop:  `{session.hard_stop_pct*100:.1f}%` drop from peak\n"
        f"Peak so far:    `{session.peak_price*100:.2f}%`"
        + (f" (stop triggers at `{(session.peak_price - session.hard_stop_pct)*100:.2f}%`)" if session.peak_price else "") + "\n"
        f"Layer size: `${session.base_size:.2f}` | "
        f"Add every: `{session.add_pct*100:.1f}%` | "
        f"Max layers: `{session.max_layers}`\n"
        f"Mode:       `{mode}`\n"
        f"Balance:    `${bal:.2f}`\n\n"
        f"**Active Layers ({len(session.layers)}):**\n{layer_lines}",
        ephemeral=True
    )

# helper used only in btcstatus
def btc_market_pct(p): 
    import btc_market as _m
    return _m.pct(p)


# ── /btctest — Polymarket connectivity diagnostic ────────────────────────────

@tree.command(name="btctest", description="[Admin] Test Polymarket connection and find the live BTC 5m slug")
@app_commands.default_permissions(manage_guild=True)
async def cmd_btctest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    import aiohttp, json
    from math import floor as _floor

    ts   = _floor(__import__('time').time() / 300) * 300
    slug = f"btc-updown-5m-{ts}"
    lines = [f"**Slug:** `{slug}`"]

    try:
        async with aiohttp.ClientSession() as sess:
            # Step 1: Gamma event
            async with sess.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                raw    = await r.json() if r.status == 200 else None
                events = (raw if isinstance(raw, list) else [raw]) if raw else []
                event  = events[0] if events else {}
                markets = event.get("markets", [])
                m = markets[0] if markets else {}
                cid     = m.get("conditionId", "")
                prices  = m.get("outcomePrices", "[]")
                outcomes = m.get("outcomes", "[]")
                lines.append(f"Gamma status: `{r.status}` | markets: `{len(markets)}`")
                lines.append(f"conditionId: `{cid}`")
                lines.append(f"outcomes: `{outcomes}` | prices: `{prices}`")

            # Step 2: CLOB token IDs
            if cid:
                async with sess.get(
                    f"https://clob.polymarket.com/markets/{cid}",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r2:
                    clob = await r2.json() if r2.status == 200 else None
                    lines.append(f"CLOB status: `{r2.status}`")
                    if clob:
                        tokens = clob.get("tokens", [])
                        lines.append(f"CLOB tokens: `{[(t.get('outcome'), t.get('token_id','')[:16]+'...') for t in tokens]}`")
                    else:
                        lines.append("CLOB returned no data")

        # Step 3: full end-to-end test
        round_info = await btc_market.get_current_round()
        if round_info:
            lines.append(f"\n✅ **get_current_round() works!**")
            lines.append(f"Up: `{btc_market.pct(round_info.up_price):.2f}%` | Down: `{btc_market.pct(round_info.down_price):.2f}%`")
            lines.append(f"Ends in: `{btc_market.seconds_until_end(round_info):.0f}s` | Entry open: `{btc_market.is_entry_open(round_info)}`")
        else:
            lines.append("\n❌ get_current_round() still returning None")

    except Exception as e:
        lines.append(f"❌ Error: `{e}`")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not set in .env")
    bot.run(token)