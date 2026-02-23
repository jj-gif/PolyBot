"""
balance.py — Reads USDC and MATIC balances from the Polygon blockchain.
No private key needed — wallet address is enough for read-only queries.
Tries multiple RPCs in order until one works.
"""
from web3 import Web3

# Multiple RPCs in fallback order — all free, no API key needed
RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]

# USDC on Polygon (native USDC, 6 decimals)
USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

ERC20_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _get_web3() -> Web3:
    """Try each RPC until one connects."""
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("All Polygon RPCs failed — try again in a moment.")


def get_balances(wallet_address: str) -> dict:
    """
    Returns USDC and MATIC balances for a wallet address.
    Read-only — no private key needed.
    """
    w3 = _get_web3()
    checksummed = Web3.to_checksum_address(wallet_address)

    # MATIC (native token)
    matic_wei = w3.eth.get_balance(checksummed)
    matic_bal = float(w3.from_wei(matic_wei, "ether"))

    # USDC (ERC-20, 6 decimals)
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI
    )
    usdc_raw = usdc_contract.functions.balanceOf(checksummed).call()
    usdc_bal = usdc_raw / 1_000_000

    return {
        "usdc":    usdc_bal,
        "matic":   matic_bal,
        "address": wallet_address,
    }