"""
Ed25519 request signer for Polymarket.US API authentication.

Auth scheme: sign UTF-8 string "{timestamp_ms}{METHOD}{path}" with an
Ed25519 private key whose seed is the first 32 bytes of base64-decoded secret.
"""

import base64
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Ed25519Signer:
    """Signs Polymarket.US API requests using Ed25519."""

    def __init__(self, key_id: str, secret_key_b64: str) -> None:
        self._key_id = key_id
        seed = base64.b64decode(secret_key_b64)[:32]
        self._private_key = Ed25519PrivateKey.from_private_bytes(seed)

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Return required auth headers for a single request."""
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        sig = base64.b64encode(self._private_key.sign(msg)).decode("ascii")
        return {
            "X-PM-Access-Key": self._key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": sig,
            "Content-Type": "application/json",
        }
