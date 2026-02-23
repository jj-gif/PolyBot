"""
trader.py - Background monitoring loop.

All trades go through the BOT wallet (BOT_WALLET_PRIVATE_KEY in .env).
Per-user virtual balances are checked before buying and updated after sells.

Dry run mode: skips real orders, logs everything to reports_channel instead.
"""
import asyncio
import os
import logging

import database as db
import polymarket_client as pm

log = logging.getLogger("trader")

# Set by bot.py after on_ready
alert_channel   = None
reports_channel = None

# Cached bot CLOB client (single shared client for all trades)
_bot_clob = None


def _get_bot_clob():
    global _bot_clob
    if _bot_clob:
        return _bot_clob
    key = os.getenv("BOT_WALLET_PRIVATE_KEY", "").strip()
    if not key:
        log.error("BOT_WALLET_PRIVATE_KEY not set — trading disabled")
        return None
    try:
        _bot_clob = pm.build_clob_client_for_key(key)
        return _bot_clob
    except Exception as e:
        log.error(f"Bot CLOB init failed: {e}")
        return None


def invalidate_clob_cache(discord_id: str = None):
    """Reset the bot CLOB client (e.g. after key change)."""
    global _bot_clob
    _bot_clob = None


async def _send(msg: str, dry_run: bool = False):
    """Send to reports channel if dry run, otherwise alert channel."""
    ch = reports_channel if dry_run else alert_channel
    if ch:
        try:
            await ch.send(msg)
        except Exception as e:
            log.error(f"Discord send failed: {e}")


# Watch checking

async def check_watches():
    watches = db.get_active_watches()
    for w in watches:
        price = await pm.get_token_price(w["token_id"])
        if price is None:
            continue
        if price <= w["buy_price"]:
            await execute_buy(w, price)


async def execute_buy(watch, current_price: float):
    discord_id = watch["discord_id"]
    token_id   = watch["token_id"]
    trade_size = watch["trade_size"]
    shares_est = round(trade_size / current_price, 4)
    tag        = f"<@{discord_id}>"

    # Check virtual balance
    user = db.get_user(discord_id)
    if not user or user["virtual_balance"] < trade_size:
        await _send(
            f"⚠️ {tag} Insufficient balance for buy — "
            f"needs `${trade_size:.2f}`, has `${(user['virtual_balance'] if user else 0):.2f}`"
        )
        return

    # Debit virtual balance
    if not db.debit_balance(discord_id, trade_size, note=f"Buy {watch['question'][:40]}"):
        await _send(f"⚠️ {tag} Balance debit failed — trade skipped.")
        return

    dry_run   = watch.get("dry_run", 0) == 1
    clob      = _get_bot_clob()
    avg_price = current_price
    shares    = shares_est
    order_id  = "dry-run" if dry_run else "paper"

    if not dry_run and clob:
        try:
            resp      = pm.place_market_buy(clob, token_id, trade_size)
            avg_price = float(resp.get("price", current_price))
            shares    = float(resp.get("size", shares_est))
            order_id  = resp.get("orderID", "unknown")
        except Exception as e:
            # Refund on failure
            db.credit_trade_profit(discord_id, trade_size, note="Buy refund (order failed)")
            await _send(f"⚠️ {tag} Buy order failed: `{e}` — balance refunded.")
            return

    prefix = "🧪 [DRY RUN] " if dry_run else ""
    await _send(
        f"{prefix}🟢 **BUY TRIGGERED** {tag}\n"
        f"**{watch['question']}** — **{watch['outcome']}**\n"
        f"Price: `{current_price:.4f}` (trigger ≤ `{watch['buy_price']:.4f}`) | "
        f"Spent: `${trade_size:.2f}` | Shares: `{shares:.4f}`",
        dry_run=dry_run
    )

    pos_id = db.open_position(
        discord_id = discord_id,
        watch_id   = watch["id"],
        token_id   = token_id,
        question   = watch["question"],
        outcome    = watch["outcome"],
        avg_price  = avg_price,
        shares     = shares,
        order_id   = order_id,
    )
    db.mark_watch_filled(watch["id"])

    await _send(
        f"{prefix}✅ Position `{pos_id}` opened for {tag} | "
        f"TP: `{watch['take_profit'] or 'off'}` | "
        f"SL: `{watch['stop_loss'] or 'off'}` | "
        f"Stop%: `{watch['stop_pct'] or 'off'}%`",
        dry_run=dry_run
    )


# Position checking

async def check_positions():
    positions = db.get_open_positions()
    for pos in positions:
        price = await pm.get_token_price(pos["token_id"])
        if price is None:
            continue

        with db.get_conn() as conn:
            watch = conn.execute(
                "SELECT * FROM watches WHERE id=?", (pos["watch_id"],)
            ).fetchone()

        avg    = pos["avg_price"]
        reason = None

        if watch and watch["take_profit"] and price >= watch["take_profit"]:
            reason = "take_profit"
        elif watch and watch["stop_loss"] and price <= watch["stop_loss"]:
            reason = "stop_loss"
        elif watch and watch["stop_pct"]:
            if price <= avg * (1 - watch["stop_pct"] / 100):
                reason = f"stop_pct ({watch['stop_pct']}% drop)"

        if reason:
            await execute_sell(pos, price, reason, watch)


async def execute_sell(pos, current_price: float, reason: str, watch=None):
    discord_id = pos["discord_id"]
    token_id   = pos["token_id"]
    shares     = pos["shares"]
    avg        = pos["avg_price"]
    cost       = pos["cost_basis"]
    proceeds   = current_price * shares
    pnl        = proceeds - cost
    pnl_pct    = ((current_price / avg) - 1) * 100 if avg else 0
    tag        = f"<@{discord_id}>"
    emoji      = "📈" if pnl >= 0 else "📉"

    dry_run = watch.get("dry_run", 0) == 1 if watch else False
    clob    = _get_bot_clob()

    if not dry_run and clob:
        try:
            pm.place_market_sell(clob, token_id, shares)
        except Exception as e:
            await _send(f"⚠️ {tag} Sell failed: `{e}`", dry_run=dry_run)
            return

    # Credit proceeds back to virtual balance
    db.credit_trade_profit(discord_id, proceeds, note=f"Sell {pos['question'][:40]}")

    prefix = "🧪 [DRY RUN] " if dry_run else ""
    await _send(
        f"{prefix}{emoji} **SELL** {tag} — **{pos['question']}**\n"
        f"Reason: `{reason}` | "
        f"Sell: `{current_price:.4f}` | Avg buy: `{avg:.4f}`\n"
        f"Proceeds: `${proceeds:.2f}` | PnL: `{'%+.2f' % pnl} USDC` (`{'%+.1f' % pnl_pct}%`)",
        dry_run=dry_run
    )

    db.close_position(pos["id"], reason)


# Main loop

async def monitor_loop():
    poll_interval = int(os.getenv("POLL_INTERVAL", 30))
    log.info(f"Monitor loop started (every {poll_interval}s)")
    while True:
        try:
            await check_watches()
            await check_positions()
        except Exception as e:
            log.error(f"Monitor loop error: {e}")
        await asyncio.sleep(poll_interval)