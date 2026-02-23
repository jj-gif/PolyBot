"""
polymarket_client.py — Wraps the Gamma market-data API and CLOB trading API.
"""
import os
import logging
import aiohttp
from typing import Optional

log = logging.getLogger("polymarket_client")

# ── Apply proxy BEFORE httpx is imported (httpx reads env vars at import time) ─
_proxy = os.getenv("CLOB_PROXY", "").strip()
if _proxy:
    os.environ["HTTPS_PROXY"] = _proxy
    os.environ["HTTP_PROXY"]  = _proxy
    os.environ["ALL_PROXY"]   = _proxy
    log.info(f"CLOB proxy set: {_proxy[:40]}...")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"


async def search_markets(query: str, limit: int = 5) -> list[dict]:
    import json as _json

    query_lower = query.lower().strip()
    results     = []
    offset      = 0
    batch_size  = 100
    max_pages   = 10

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        for _ in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit":  batch_size,
                "offset": offset,
            }
            try:
                async with session.get(
                    f"{GAMMA_BASE}/markets", params=params
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json(content_type=None)
            except Exception as e:
                print(f"[search error] {e}")
                break

            if isinstance(data, dict):
                data = data.get("markets", [])
            if not data:
                break

            for m in data:
                question = m.get("question", "") or ""

                # Skip if doesn't match search
                if query_lower not in question.lower():
                    continue

                # ── Token IDs — stored as a JSON string e.g. '["123","456"]'
                raw_clob = m.get("clobTokenIds") or "[]"
                if isinstance(raw_clob, str):
                    try:
                        token_ids = _json.loads(raw_clob)
                    except Exception:
                        token_ids = []
                else:
                    token_ids = raw_clob

                # ── Outcome names — also a JSON string e.g. '["Yes","No"]'
                raw_outcomes = m.get("outcomes") or "[]"
                if isinstance(raw_outcomes, str):
                    try:
                        outcomes = _json.loads(raw_outcomes)
                    except Exception:
                        outcomes = []
                else:
                    outcomes = raw_outcomes

                # ── Prices — also a JSON string e.g. '["0.45","0.55"]'
                raw_prices = m.get("outcomePrices") or "[]"
                if isinstance(raw_prices, str):
                    try:
                        prices = _json.loads(raw_prices)
                    except Exception:
                        prices = []
                else:
                    prices = raw_prices

                parsed_tokens = []
                for i, token_id in enumerate(token_ids):
                    if not token_id:
                        continue
                    outcome = outcomes[i] if i < len(outcomes) else f"Option {i+1}"
                    try:
                        price = float(prices[i]) if i < len(prices) else 0.0
                    except (ValueError, TypeError):
                        price = 0.0

                    parsed_tokens.append({
                        "token_id": str(token_id),
                        "outcome":  outcome,
                        "price":    price,
                    })

                if parsed_tokens:
                    results.append({
                        "condition_id": m.get("conditionId") or "",
                        "question":     question,
                        "end_date":     m.get("endDateIso") or m.get("endDate") or "",
                        "tokens":       parsed_tokens,
                    })

                if len(results) >= limit:
                    return results

            if len(data) < batch_size:
                break
            offset += batch_size

    return results


async def get_token_price(token_id: str) -> Optional[float]:
    url = f"{CLOB_BASE}/price"
    params = {"token_id": token_id, "side": "buy"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
    except Exception as e:
        print(f"[price fetch error] {token_id}: {e}")
    return None


# ── CLOB Trading ──────────────────────────────────────────────────────────────

def build_clob_client_for_key(private_key: str):
    """Build a CLOB client for a specific private key."""
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    proxy = os.getenv("CLOB_PROXY", "").strip() or None

    # Pass proxy directly to ClobClient if supported, otherwise rely on env vars
    try:
        client = ClobClient(host=CLOB_BASE, key=private_key, chain_id=POLYGON,
                            proxy=proxy)
    except TypeError:
        # Older versions don't accept proxy kwarg — env vars handle it
        client = ClobClient(host=CLOB_BASE, key=private_key, chain_id=POLYGON)

    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def build_clob_client():
    """Build a CLOB client from the PRIVATE_KEY env var (bot owner)."""
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise ValueError("PRIVATE_KEY not set in environment")
    return build_clob_client_for_key(private_key)


async def _post_order_via_aiohttp(client, signed_order) -> dict:
    """
    Post a signed order to Polymarket using aiohttp instead of httpx,
    so the CLOB_PROXY is properly respected.
    """
    proxy = os.getenv("CLOB_PROXY", "").strip() or None
    creds = client.creds

    # Serialize the signed order — py_clob_client uses dataclasses/custom objects
    def serialize(obj):
        if hasattr(obj, "__dict__"):
            return {k: serialize(v) for k, v in obj.__dict__.items()}
        elif hasattr(obj, "_asdict"):
            return {k: serialize(v) for k, v in obj._asdict().items()}
        elif isinstance(obj, (list, tuple)):
            return [serialize(i) for i in obj]
        else:
            return obj

    order_dict = serialize(signed_order)

    # Polymarket expects this envelope
    payload = {
        "order":     order_dict,
        "owner":     creds.api_key,
        "orderType": "FOK",
    }

    # Auth headers
    headers = {
        "Content-Type":   "application/json",
        "POLY_ADDRESS":   creds.api_key,
        "POLY_SIGNATURE": creds.api_secret,
        "POLY_TIMESTAMP": creds.api_passphrase,
        "POLY_NONCE":     "",
    }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{CLOB_BASE}/order",
            json=payload,
            headers=headers,
            proxy=proxy,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status not in (200, 201):
                raise Exception(f"Order POST failed {resp.status}: {data}")
            return data


def place_market_buy(client, token_id: str, usdc_amount: float, price: float = 0.5) -> dict:
    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import BUY
    import asyncio

    order_args = MarketOrderArgs(token_id=token_id, amount=usdc_amount, side=BUY)
    signed = client.create_market_order(order_args)

    # Use our aiohttp poster instead of client.post_order()
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_post_order_via_aiohttp(client, signed))
    except RuntimeError:
        # Already in async context — caller should use await
        raise RuntimeError("place_market_buy called from async context — use await place_market_buy_async()")


async def place_market_buy_async(client, token_id: str, usdc_amount: float, price: float = 0.5) -> dict:
    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import BUY

    order_args = MarketOrderArgs(token_id=token_id, amount=usdc_amount, side=BUY)
    signed = client.create_market_order(order_args)
    return await _post_order_via_aiohttp(client, signed)


async def place_market_sell_async(client, token_id: str, shares: float) -> dict:
    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import SELL

    order_args = MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
    signed = client.create_market_order(order_args)
    return await _post_order_via_aiohttp(client, signed)


def place_market_sell(client, token_id: str, shares: float) -> dict:
    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import SELL
    import asyncio

    order_args = MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
    signed = client.create_market_order(order_args)
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_post_order_via_aiohttp(client, signed))
    except RuntimeError:
        raise RuntimeError("place_market_sell called from async context — use await place_market_sell_async()")


def get_wallet_address_from_key(private_key: str) -> str:
    """Derive the public wallet address from a private key."""
    from eth_account import Account
    account = Account.from_key(private_key)
    return account.address