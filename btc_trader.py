"""
btc_trader.py - BTC Up/Down 5-minute auto-trader.

User picks a side (Up or Down) at session start — bot only trades that side.

Layer-based position tracking:
  - Each buy creates a layer with its own entry price and shares
  - As price rises above the last layer by add_pct%, a new layer is added
  - On the way down, layers are sold LIFO (last in, first out)
  - If price drops below buy_threshold at any point, ALL layers sold immediately

No flipping between sides. Simple, predictable.
"""
import asyncio
import logging
import time
import os
from dataclasses import dataclass, field
from typing import Optional

import discord

import btc_market as mkt
import database as db
import polymarket_client as pm

log = logging.getLogger("btc_trader")

POLL_SECONDS = 3
SAVINGS_PCT  = 0.02

alert_channel:   Optional[discord.TextChannel] = None
reports_channel: Optional[discord.TextChannel] = None

_sessions: dict[str, "BtcSession"] = {}
_bot_clob = None


def _get_bot_clob():
    global _bot_clob
    if _bot_clob:
        return _bot_clob
    key = os.getenv("BOT_WALLET_PRIVATE_KEY", "").strip()
    if not key:
        log.error("BOT_WALLET_PRIVATE_KEY not set")
        return None
    try:
        _bot_clob = pm.build_clob_client_for_key(key)
        return _bot_clob
    except Exception as e:
        log.error(f"Bot CLOB init failed: {e}")
        return None


# ── Layer dataclass ───────────────────────────────────────────────────────────

@dataclass
class Layer:
    """One individual buy entry."""
    entry_price: float   # price at which this layer was bought
    shares:      float   # shares held in this layer
    size_usdc:   float   # USDC spent on this layer


# ── Session dataclass ─────────────────────────────────────────────────────────

@dataclass
class BtcSession:
    discord_id:      str
    buy_threshold:   float  # e.g. 0.60 — buy whichever side hits this
    hard_stop_pct:   float  # e.g. 0.05 — sell when price drops this % from peak
    base_size:       float  # USDC per layer
    add_pct:         float  # add a new layer every this many % rise
    max_layers:      int
    full_auto:       bool
    max_rounds:      int
    dry_run:         bool   = False
    dry_run_balance: float  = 100.0
    rounds_done:     int    = 0
    savings_wallet:  str    = ""
    # Runtime state
    active_side:     str    = ""     # "Up" or "Down" — whichever side is currently held
    layers:          list   = field(default_factory=list)
    peak_price:      float  = 0.0
    re_entry_state:  str    = ""     # "" | "waiting_5s" | "waiting_for_dip" | "waiting_for_rise"
    re_entry_wait_until: float = 0.0
    last_buy_time:   float  = 0.0
    round_condition: str    = ""
    round_closed:    bool   = False
    stopped:         bool   = False


def get_session(discord_id: str) -> Optional[BtcSession]:
    return _sessions.get(discord_id)


def start_session(session: BtcSession):
    _sessions[session.discord_id] = session
    log.info(f"Session started for {session.discord_id} threshold={session.buy_threshold} dry={session.dry_run}")


def stop_session(discord_id: str):
    _sessions.pop(discord_id, None)
    log.info(f"Session stopped for {discord_id}")


# ── Alert helpers ─────────────────────────────────────────────────────────────

async def _send(msg: str, dry_run: bool = False):
    ch = reports_channel if dry_run else alert_channel
    if ch:
        try:
            await ch.send(msg)
        except Exception as e:
            log.error(f"Alert send failed: {e}")


# ── Savings transfer ──────────────────────────────────────────────────────────

async def _send_savings(session: BtcSession, profit_usdc: float):
    if not session.savings_wallet or profit_usdc <= 0:
        return
    amount = round(profit_usdc * SAVINGS_PCT, 4)
    if amount < 0.01:
        return

    if session.dry_run:
        await _send(
            f"🧪 [DRY RUN] 🏦 Would send `${amount:.4f}` to savings `{session.savings_wallet[:10]}...`",
            dry_run=True
        )
        return

    try:
        from web3 import Web3
        from eth_account import Account
        USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        ABI  = [{"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
                 "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
                 "stateMutability": "nonpayable", "type": "function"}]
        for url in ["https://rpc.ankr.com/polygon", "https://polygon.llamarpc.com", "https://polygon-rpc.com"]:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
                if w3.is_connected():
                    break
            except Exception:
                continue
        bot_key  = os.getenv("BOT_WALLET_PRIVATE_KEY", "").strip()
        account  = Account.from_key(bot_key)
        contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
        tx = contract.functions.transfer(
            Web3.to_checksum_address(session.savings_wallet), int(amount * 1_000_000)
        ).build_transaction({
            "from": account.address, "nonce": w3.eth.get_transaction_count(account.address),
            "gasPrice": w3.eth.gas_price, "chainId": 137,
        })
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).rawTransaction).hex()
        await _send(f"🏦 Savings `${amount:.4f}` sent → `{session.savings_wallet[:10]}...` | Tx: `{tx_hash}`")
    except Exception as e:
        log.error(f"Savings failed: {e}")
        await _send(f"⚠️ <@{session.discord_id}> Savings transfer failed: `{e}`")


# ── Reset between rounds ──────────────────────────────────────────────────────

def _reset_round(session: BtcSession, condition_id: str):
    session.layers               = []
    session.peak_price           = 0.0
    session.active_side          = ""
    session.re_entry_state       = ""
    session.re_entry_wait_until  = 0.0
    session.last_buy_time        = 0.0
    session.round_closed         = False
    session.round_condition      = condition_id


# ── Buy a new layer ───────────────────────────────────────────────────────────

async def _buy_layer(session: BtcSession, round_info: mkt.RoundInfo, reason: str):
    """Place one buy and append a Layer to the session."""
    import time

    # Rate limit — minimum 30 seconds between any buys
    BUY_COOLDOWN = 30
    now = time.time()
    if now - session.last_buy_time < BUY_COOLDOWN:
        return  # silently skip — not spam, just waiting

    token_id   = round_info.up_token_id if session.active_side == "Up" else round_info.down_token_id
    price      = round_info.up_price    if session.active_side == "Up" else round_info.down_price
    size       = session.base_size
    shares_est = round(size / price, 4)
    prefix     = "🧪 [DRY RUN] " if session.dry_run else ""

    # Check and debit balance — fake for dry run, real for live
    if session.dry_run:
        if session.dry_run_balance < size:
            await _send(
                f"{prefix}⚠️ <@{session.discord_id}> Insufficient simulated balance "
                f"(need `${size:.2f}`, have `${session.dry_run_balance:.2f}`) — skipping.",
                dry_run=True
            )
            return
        session.dry_run_balance -= size
    else:
        user = db.get_user(session.discord_id)
        if not user or user["virtual_balance"] < size:
            await _send(
                f"⚠️ <@{session.discord_id}> Insufficient balance "
                f"(need `${size:.2f}`, have `${(user['virtual_balance'] if user else 0):.2f}`) — skipping."
            )
            return
        if not db.debit_balance(session.discord_id, size, note=f"BTC {session.active_side} layer buy"):
            await _send(f"⚠️ <@{session.discord_id}> Balance debit failed.")
            return

    fill_price = price

    if not session.dry_run:
        clob = _get_bot_clob()
        if clob:
            # Retry up to 3 times on "no match" (temporary liquidity gap)
            last_err = None
            for attempt in range(3):
                try:
                    resp       = await pm.place_market_buy_async(clob, token_id, size, price)
                    fill_price = float(resp.get("price", price))
                    shares_est = float(resp.get("size", shares_est))
                    last_err   = None
                    break
                except Exception as e:
                    last_err = e
                    if "no match" in str(e).lower() and attempt < 2:
                        log.info(f"No match on attempt {attempt+1}, retrying in 3s...")
                        await asyncio.sleep(3)
                    else:
                        break

            if last_err is not None:
                if session.dry_run:
                    session.dry_run_balance += size
                else:
                    db.credit_trade_profit(session.discord_id, size, note="Layer buy refund")
                await _send(f"⚠️ <@{session.discord_id}> Buy failed after retries: `{last_err}` — refunded.")
                return

    layer = Layer(entry_price=fill_price, shares=shares_est, size_usdc=size)
    session.layers.append(layer)
    session.last_buy_time = time.time()

    bal       = session.dry_run_balance if session.dry_run else (db.get_user(session.discord_id) or {}).get("virtual_balance", 0)
    layer_num = len(session.layers)

    await _send(
        f"{prefix}🟢 **BUY {session.active_side.upper()} — Layer {layer_num}** <@{session.discord_id}>\n"
        f"Reason: `{reason}`\n"
        f"Entry: `{mkt.pct(fill_price):.2f}%` | Size: `${size:.2f}` | Shares: `{shares_est:.4f}`\n"
        f"Active layers: `{layer_num}` | Balance: `${bal:.2f}`\n"
        f"⏱ `{mkt.seconds_until_end(round_info):.0f}s` left",
        dry_run=session.dry_run
    )


# ── Sell ALL layers (trailing stop) ──────────────────────────────────────────

async def _sell_all(session: BtcSession, round_info: mkt.RoundInfo, reason: str, from_trailing_stop: bool = False):
    """Emergency sell — liquidate every layer at once."""
    if not session.layers:
        return

    token_id   = round_info.up_token_id if session.active_side == "Up" else round_info.down_token_id
    price      = round_info.up_price    if session.active_side == "Up" else round_info.down_price
    total_shares = sum(l.shares for l in session.layers)
    total_cost   = sum(l.size_usdc for l in session.layers)
    proceeds     = price * total_shares
    pnl          = proceeds - total_cost
    prefix       = "🧪 [DRY RUN] " if session.dry_run else ""

    if not session.dry_run:
        clob = _get_bot_clob()
        if clob:
            try:
                await pm.place_market_sell_async(clob, token_id, total_shares)
            except Exception as e:
                await _send(f"⚠️ <@{session.discord_id}> HARD STOP sell failed: `{e}`")
                return
        db.credit_trade_profit(session.discord_id, proceeds, note=f"BTC {session.active_side} trailing stop")
    else:
        session.dry_run_balance += proceeds

    layer_count    = len(session.layers)
    session.layers = []
    prev_side      = session.active_side
    session.active_side = ""

    bal = session.dry_run_balance if session.dry_run else (db.get_user(session.discord_id) or {}).get("virtual_balance", 0)

    if from_trailing_stop:
        # Trailing stop — wait 5s then check if either side is still above threshold
        footer = f"⏳ Checking re-entry in 5s — will buy back if either side above `{mkt.pct(session.buy_threshold):.2f}%`..."
        session.peak_price          = 0.0
        session.re_entry_state      = "waiting_5s"
        session.re_entry_wait_until = time.time() + 5
    else:
        # Threshold floor — price already at/below threshold, go straight to watching for a rise
        footer = f"👀 Watching for **Up** or **Down** to rise back above `{mkt.pct(session.buy_threshold):.2f}%`..."
        session.peak_price     = 0.0
        session.re_entry_state = "waiting_for_rise"

    await _send(
        f"{prefix}🔴 **SOLD — ALL {layer_count} LAYERS** ({prev_side}) <@{session.discord_id}>\n"
        f"Reason: `{reason}`\n"
        f"Sold at: `{mkt.pct(price):.2f}%` | "
        f"Proceeds: `${proceeds:.4f}` | PnL: `{'%+.4f' % pnl} USDC` | "
        f"Balance: `${bal:.2f}`\n"
        f"{footer}",
        dry_run=session.dry_run
    )


# ── Per-tick logic ────────────────────────────────────────────────────────────

async def _process_session(session: BtcSession, round_info: mkt.RoundInfo):
    up_price   = round_info.up_price
    down_price = round_info.down_price
    threshold  = session.buy_threshold
    entry_open = mkt.is_entry_open(round_info)
    prefix     = "🧪 [DRY RUN] " if session.dry_run else ""

    # ── TRAILING STOP RE-ENTRY: 5s cooldown, then check both sides ───────────
    if session.re_entry_state == "waiting_5s":
        if time.time() < session.re_entry_wait_until:
            return
        best_side, best_price = _best_side(up_price, down_price, threshold)
        if best_side and entry_open:
            session.re_entry_state = ""
            session.active_side    = best_side
            session.peak_price     = best_price
            await _send(
                f"{prefix}↩️ <@{session.discord_id}> **{best_side}** still at "
                f"`{mkt.pct(best_price):.2f}%` after 5s — re-entering...",
                dry_run=session.dry_run
            )
            await _buy_layer(session, round_info, "Re-entry after trailing stop (5s check)")
        else:
            # Neither side above threshold — wait for a rise
            session.re_entry_state = "waiting_for_rise"
            await _send(
                f"{prefix}📉 <@{session.discord_id}> Neither side above "
                f"`{mkt.pct(threshold):.2f}%` — watching for a rise...",
                dry_run=session.dry_run
            )
        return

    # ── WATCHING FOR RISE (after any sell) ───────────────────────────────────
    if session.re_entry_state == "waiting_for_rise":
        best_side, best_price = _best_side(up_price, down_price, threshold)
        if best_side and entry_open:
            session.re_entry_state = ""
            session.active_side    = best_side
            session.peak_price     = best_price
            await _buy_layer(
                session, round_info,
                f"Re-entry: **{best_side}** rose to `{mkt.pct(best_price):.2f}%`"
            )
        return

    # ── HOLDING: update peak and check exits ─────────────────────────────────
    if session.layers and session.active_side:
        price = up_price if session.active_side == "Up" else down_price

        if price > session.peak_price:
            session.peak_price = price

        # THRESHOLD FLOOR: dropped back to/below entry price → sell, watch for rise
        entry_floor = session.layers[0].entry_price
        if price <= entry_floor:
            await _sell_all(
                session, round_info,
                f"{session.active_side} dropped back to entry `{mkt.pct(price):.2f}%` (bought at `{mkt.pct(entry_floor):.2f}%`)",
                from_trailing_stop=False
            )
            return

        # TRAILING STOP: dropped hard_stop_pct from peak → sell, 5s check
        drop = session.peak_price - price
        if drop >= session.hard_stop_pct:
            await _sell_all(
                session, round_info,
                f"{session.active_side} dropped `{drop*100:.2f}%` from peak "
                f"`{mkt.pct(session.peak_price):.2f}%` → `{mkt.pct(price):.2f}%`",
                from_trailing_stop=True
            )
            return

        # Add more layers as price rises
        if entry_open and len(session.layers) < session.max_layers:
            last_price = session.layers[-1].entry_price
            if price >= last_price + session.add_pct:
                await _buy_layer(
                    session, round_info,
                    f"{session.active_side} rose to `{mkt.pct(price):.2f}%` "
                    f"(+`{(price - last_price)*100:.2f}%` from last layer)"
                )
        return

    # ── NO POSITION: watch for either side to hit threshold ──────────────────
    if not entry_open:
        return
    best_side, best_price = _best_side(up_price, down_price, threshold)
    if best_side:
        session.active_side = best_side
        session.peak_price  = best_price
        await _buy_layer(
            session, round_info,
            f"**{best_side}** hit `{mkt.pct(best_price):.2f}%` ≥ threshold `{mkt.pct(threshold):.2f}%`"
        )


def _best_side(up_price: float, down_price: float, threshold: float) -> tuple[str, float]:
    """Return whichever side is at or above threshold (highest wins). ('', 0) if neither."""
    candidates = []
    if up_price   >= threshold: candidates.append(("Up",   up_price))
    if down_price >= threshold: candidates.append(("Down", down_price))
    if not candidates:
        return "", 0.0
    return max(candidates, key=lambda x: x[1])


# ── Round end ─────────────────────────────────────────────────────────────────

async def _handle_round_end(session: BtcSession, round_info: mkt.RoundInfo):
    """Called when entry closes (30s left). Sell everything at current price."""
    if not session.layers:
        _reset_round(session, "")
        session.rounds_done += 1
        prefix = "🧪 [DRY RUN] " if session.dry_run else ""
        await _send(
            f"{prefix}⏱ <@{session.discord_id}> Round closing — no position held.",
            dry_run=session.dry_run
        )
    else:
        await _sell_all(
            session, round_info,
            "Round closing — 30s left, selling before resolution"
        )
        session.rounds_done += 1

    _reset_round(session, "")

    if not session.full_auto and session.rounds_done >= session.max_rounds:
        prefix = "🧪 [DRY RUN] " if session.dry_run else ""
        await _send(
            f"{prefix}🛑 <@{session.discord_id}> Reached `{session.max_rounds}` rounds — stopping.",
            dry_run=session.dry_run
        )
        session.stopped = True


# ── Main loop ─────────────────────────────────────────────────────────────────

async def monitor_loop():
    log.info(f"BTC monitor loop started (every {POLL_SECONDS}s)")
    current_round: Optional[mkt.RoundInfo] = None
    last_heartbeat = 0.0

    while True:
        try:
            # Heartbeat every 60s
            if time.time() - last_heartbeat > 60:
                active = [s for s in _sessions.values() if not s.stopped]
                log.info(f"Monitor heartbeat | sessions={len(active)} | round={'active' if current_round else 'none'}")
                last_heartbeat = time.time()

            if current_round is None:
                current_round = await mkt.get_current_round()
                if current_round:
                    log.info(f"New round: {current_round.condition_id}")
                    await _send(
                        f"🔔 **New BTC 5m round!**\n"
                        f"Up: `{mkt.pct(current_round.up_price):.2f}%` | "
                        f"Down: `{mkt.pct(current_round.down_price):.2f}%` | "
                        f"⏱ `{mkt.seconds_until_end(current_round):.0f}s`"
                    )
                    for s in list(_sessions.values()):
                        if not s.stopped:
                            _reset_round(s, current_round.condition_id)
            else:
                current_round = await mkt.refresh_prices(current_round)

            # Trigger sell at entry close (30s left) — once per round per session
            if current_round and not mkt.is_entry_open(current_round):
                condition_id = current_round.condition_id
                for s in list(_sessions.values()):
                    if not s.stopped and s.round_condition == condition_id and not s.round_closed:
                        s.round_closed = True
                        await _handle_round_end(s, current_round)

            # Reset when round fully expires
            if current_round and mkt.seconds_until_end(current_round) <= 0:
                current_round = None
                await asyncio.sleep(5)
                continue

            if current_round:
                active = [s for s in _sessions.values() if not s.stopped]
                if active:
                    log.info(
                        f"Tick | Up={mkt.pct(current_round.up_price):.2f}% "
                        f"Down={mkt.pct(current_round.down_price):.2f}% | "
                        f"ends in {mkt.seconds_until_end(current_round):.0f}s | "
                        f"sessions={len(active)}"
                    )
                for s in list(_sessions.values()):
                    if s.stopped:
                        _sessions.pop(s.discord_id, None)
                        continue
                    try:
                        await _process_session(s, current_round)
                    except Exception as e:
                        log.error(f"Session error {s.discord_id}: {e}")

        except Exception as e:
            log.error(f"BTC monitor loop error: {e}")

        await asyncio.sleep(POLL_SECONDS)