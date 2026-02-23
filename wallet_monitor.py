"""
wallet_monitor.py — On-demand deposit watcher.

Watches the bot wallet's USDC balance. When it goes up, scans recent
Transfer logs to find who sent it and credits the right user.
No complex block range queries — just balance polling + small log scan.
"""
import os
import asyncio
import logging
from web3 import Web3

import database as db

log = logging.getLogger("wallet_monitor")

RPC_URLS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://1rpc.io/matic",
]

USDC_ADDRESS   = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_ABI       = [{
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}]
MIN_DEPOSIT  = 1.0
POLL_SECONDS = 8
TIMEOUT_MINS = 15

_active: dict[str, bool] = {}


def _get_web3() -> Web3:
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("All Polygon RPCs failed")


def _get_usdc_balance(w3: Web3, address: str) -> float:
    """Return USDC balance of address in human units."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI
    )
    raw = contract.functions.balanceOf(
        Web3.to_checksum_address(address)
    ).call()
    return raw / 1_000_000


def _pad_address(address: str) -> str:
    return "0x" + address[2:].lower().zfill(64)


def _find_incoming_transfer(w3: Web3, to_address: str,
                             from_block: int) -> tuple[str, float, str] | None:
    """
    Scan the last 100 blocks for any USDC Transfer TO to_address.
    Returns (from_address, amount_usdc, tx_hash) or None.
    """
    try:
        latest    = w3.eth.block_number
        scan_from = max(from_block, latest - 100)   # never more than 100 blocks

        logs = w3.eth.get_logs({
            "fromBlock": scan_from,
            "toBlock":   latest,
            "address":   Web3.to_checksum_address(USDC_ADDRESS),
            "topics": [
                TRANSFER_TOPIC,
                None,                          # any sender
                _pad_address(to_address),      # must be sent TO bot wallet
            ]
        })

        for event in reversed(logs):           # newest first
            tx_hash = event["transactionHash"].hex()
            if db.is_tx_seen(tx_hash):
                continue

            # Decode amount
            try:
                amount_raw  = int(event["data"].hex(), 16)
            except Exception:
                continue
            amount_usdc = amount_raw / 1_000_000
            if amount_usdc < MIN_DEPOSIT:
                continue

            # Decode sender from topics[1]
            try:
                sender = "0x" + event["topics"][1].hex()[-40:]
                sender = Web3.to_checksum_address(sender)
            except Exception:
                continue

            return (sender, amount_usdc, tx_hash)

    except Exception as e:
        log.warning(f"Transfer scan error: {e}")

    return None


async def watch_for_deposit(discord_id: str, interaction_followup):
    """
    Called by /deposit.
    Polls the bot wallet USDC balance every POLL_SECONDS.
    When balance increases, finds who sent it and credits the user.
    Times out after TIMEOUT_MINS.
    """
    if discord_id in _active:
        return

    bot_address = os.getenv("BOT_WALLET_ADDRESS", "").strip()
    if not bot_address:
        log.error("BOT_WALLET_ADDRESS not set")
        return

    _active[discord_id] = True
    log.info(f"Deposit watch started for {discord_id}")

    try:
        w3              = await asyncio.get_event_loop().run_in_executor(None, _get_web3)
        start_balance   = await asyncio.get_event_loop().run_in_executor(
            None, _get_usdc_balance, w3, bot_address
        )
        start_block     = await asyncio.get_event_loop().run_in_executor(
            None, lambda: w3.eth.block_number
        )
    except Exception as e:
        log.error(f"RPC connect failed: {e}")
        _active.pop(discord_id, None)
        return

    log.info(f"Bot wallet balance at start: ${start_balance:.2f} USDC | block {start_block}")
    deadline = asyncio.get_event_loop().time() + (TIMEOUT_MINS * 60)

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_SECONDS)

        try:
            current_balance = await asyncio.get_event_loop().run_in_executor(
                None, _get_usdc_balance, w3, bot_address
            )
        except Exception:
            continue

        if current_balance <= start_balance:
            continue   # nothing arrived yet

        # Balance went up — find who sent it
        increase = current_balance - start_balance
        log.info(f"Balance increased by ${increase:.2f} — scanning for sender")

        transfer = await asyncio.get_event_loop().run_in_executor(
            None, _find_incoming_transfer, w3, bot_address, start_block
        )

        if transfer:
            sender, amount_usdc, tx_hash = transfer
        else:
            # Fallback: couldn't find the tx but balance did go up — credit the increase
            sender      = "unknown"
            amount_usdc = increase
            tx_hash     = f"auto-{discord_id}-{int(asyncio.get_event_loop().time())}"

        # Credit the user who ran /deposit
        db.credit_balance(
            discord_id = discord_id,
            amount     = amount_usdc,
            tx_hash    = tx_hash,
            note       = f"Deposit from {sender[:10]}..."
        )

        user        = db.get_user(discord_id)
        new_balance = user["virtual_balance"] if user else amount_usdc

        log.info(f"Deposit credited: ${amount_usdc:.2f} from {sender[:10]}... to {discord_id}")

        try:
            await interaction_followup.send(
                f"💵 **Deposit received!** <@{discord_id}>\n"
                f"Amount: `${amount_usdc:.2f} USDC`\n"
                f"From: `{sender}`\n"
                f"New balance: `${new_balance:.2f}`\n"
                f"Tx: `{tx_hash}`",
                ephemeral=True
            )
        except Exception:
            pass

        break

    else:
        log.info(f"Deposit watch timed out for {discord_id}")
        try:
            await interaction_followup.send(
                f"⏰ Deposit window expired (15 minutes). "
                f"Run `/deposit` again when ready.",
                ephemeral=True
            )
        except Exception:
            pass

    _active.pop(discord_id, None)


def is_watching(discord_id: str) -> bool:
    return discord_id in _active