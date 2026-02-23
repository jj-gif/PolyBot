"""
crypto_utils.py — AES encryption for storing user private keys.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
The MASTER_ENCRYPTION_KEY in .env is the only way to decrypt stored keys —
if it's lost, all stored keys are unrecoverable.
"""
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


def _get_fernet() -> Fernet:
    """
    Derive a Fernet key from the MASTER_ENCRYPTION_KEY env var.
    Uses PBKDF2 so even weak passphrases produce a strong key.
    """
    master = os.getenv("MASTER_ENCRYPTION_KEY", "").encode()
    if not master:
        raise ValueError(
            "MASTER_ENCRYPTION_KEY not set in .env — "
            "generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Fixed salt — changing this breaks all stored keys
    salt = b"polymarket_bot_salt_v1"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(master))
    return Fernet(key)


def encrypt_key(private_key: str) -> str:
    """Encrypt a private key string. Returns a base64 ciphertext string."""
    f = _get_fernet()
    return f.encrypt(private_key.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a previously encrypted private key."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def validate_private_key(key: str) -> bool:
    """Basic sanity check — must be 0x-prefixed 64-char hex."""
    key = key.strip()
    if key.startswith("0x"):
        key = key[2:]
    return len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key)
