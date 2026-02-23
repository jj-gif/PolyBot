"""
btc_market.py — Discovers and tracks the live BTC Up/Down 5-minute round.

Slug formula: btc-updown-5m-{floor(unix_time/300)*300}

Token IDs live in the CLOB API, not the Gamma events API.
Prices come from outcomePrices in the Gamma market response (fast),
with CLOB price fallback for accuracy.
"""
import time
import aiohttp
import logging
from math import floor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("btc_market")

GAMMA_BASE           = "https://gamma-api.polymarket.com"
CLOB_BASE            = "https://clob.polymarket.com"
ENTRY_CUTOFF_SECONDS = 30


@dataclass
class RoundInfo:
    condition_id:  str
    question:      str
    end_timestamp: float
    up_token_id:   str
    down_token_id: str
    up_price:      float
    down_price:    float
    resolved:      bool = False
    winning_side:  Optional[str] = None


def _current_slug() -> str:
    ts = floor(time.time() / 300) * 300
    return f"btc-updown-5m-{ts}"


def _next_slug() -> str:
    ts = floor(time.time() / 300) * 300 + 300
    return f"btc-updown-5m-{ts}"


async def _fetch_json(url: str, params: dict = None):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params,
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        log.warning(f"HTTP error {url}: {e}")
    return None


async def _get_token_ids(condition_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Fetch Up/Down token IDs from the CLOB API using conditionId.
    Returns (up_token_id, down_token_id) or (None, None) on failure.
    """
    data = await _fetch_json(f"{CLOB_BASE}/markets/{condition_id}")
    if not data:
        return None, None

    tokens = data.get("tokens", [])
    if not tokens:
        return None, None

    up_token   = next((t for t in tokens if "up"   in t.get("outcome", "").lower()), None)
    down_token = next((t for t in tokens if "down" in t.get("outcome", "").lower()), None)

    up_id   = up_token.get("token_id")   if up_token   else None
    down_id = down_token.get("token_id") if down_token else None

    return up_id, down_id


async def _get_price(token_id: str) -> Optional[float]:
    data = await _fetch_json(f"{CLOB_BASE}/price",
                             {"token_id": token_id, "side": "buy"})
    if data:
        try:
            return float(data.get("price", 0))
        except Exception:
            pass
    return None


async def _round_from_slug(slug: str) -> Optional[RoundInfo]:
    """Fetch a round by slug, get token IDs from CLOB, prices from CLOB."""
    data = await _fetch_json(f"{GAMMA_BASE}/events", {"slug": slug})
    if not data:
        return None

    events  = data if isinstance(data, list) else [data]
    if not events:
        return None

    event   = events[0]
    markets = event.get("markets", [])
    now     = time.time()

    for m in markets:
        try:
            end_str = m.get("endDate") or ""
            if not end_str:
                continue
            end_ts = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")
            ).timestamp()
            if end_ts <= now:
                continue

            condition_id = m.get("conditionId", "")
            if not condition_id:
                continue

            # Prices are right in the Gamma response — use them directly
            outcome_prices = m.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                import json
                outcome_prices = json.loads(outcome_prices)
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)

            up_price   = 0.5
            down_price = 0.5
            for i, outcome in enumerate(outcomes):
                if "up" in outcome.lower() and i < len(outcome_prices):
                    up_price   = float(outcome_prices[i])
                elif "down" in outcome.lower() and i < len(outcome_prices):
                    down_price = float(outcome_prices[i])

            # Token IDs come from CLOB API
            up_token_id, down_token_id = await _get_token_ids(condition_id)

            if not up_token_id or not down_token_id:
                log.warning(f"Could not get token IDs from CLOB for {condition_id}")
                continue

            log.info(f"Round found: {slug} | ends in {end_ts - now:.0f}s | "
                     f"Up={up_price:.3f} Down={down_price:.3f}")

            return RoundInfo(
                condition_id  = condition_id,
                question      = m.get("question", "BTC Up/Down 5m"),
                end_timestamp = end_ts,
                up_token_id   = up_token_id,
                down_token_id = down_token_id,
                up_price      = up_price,
                down_price    = down_price,
            )

        except Exception as e:
            log.warning(f"Error parsing market in {slug}: {e}")
            continue

    return None


async def get_current_round() -> Optional[RoundInfo]:
    """Find the active round by calculating slug from current timestamp."""
    for slug in [_current_slug(), _next_slug()]:
        log.info(f"Trying slug: {slug}")
        result = await _round_from_slug(slug)
        if result:
            return result
    log.warning("No active BTC round found")
    return None


async def get_round_outcome(condition_id: str) -> Optional[str]:
    data = await _fetch_json(f"{GAMMA_BASE}/markets",
                             {"conditionId": condition_id})
    if not data:
        return None
    markets = data if isinstance(data, list) else [data]
    for m in markets:
        if m.get("conditionId") == condition_id and m.get("resolved"):
            outcomes       = m.get("outcomes", "[]")
            outcome_prices = m.get("outcomePrices", "[]")
            if isinstance(outcomes, str):
                import json; outcomes = json.loads(outcomes)
            if isinstance(outcome_prices, str):
                import json; outcome_prices = json.loads(outcome_prices)
            for i, outcome in enumerate(outcomes):
                if i < len(outcome_prices) and float(outcome_prices[i]) >= 0.99:
                    if "up"   in outcome.lower(): return "Up"
                    if "down" in outcome.lower(): return "Down"
    return None


async def refresh_prices(round_info: RoundInfo) -> RoundInfo:
    """Refresh prices — use CLOB for accuracy."""
    up_price   = await _get_price(round_info.up_token_id)
    down_price = await _get_price(round_info.down_token_id)
    round_info.up_price   = up_price   or round_info.up_price
    round_info.down_price = down_price or round_info.down_price
    return round_info


def seconds_until_end(round_info: RoundInfo) -> float:
    return round_info.end_timestamp - time.time()


def is_entry_open(round_info: RoundInfo) -> bool:
    return seconds_until_end(round_info) > ENTRY_CUTOFF_SECONDS


def pct(price: float) -> float:
    return round(price * 100, 2)