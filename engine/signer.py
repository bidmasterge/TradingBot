"""Request signing for the Binance WebSocket API.

HMAC-SHA256 secrets must be sent on every order.
Ed25519 keys can log in once with session.logon, which is the faster path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Protocol


class Signer(Protocol):
    kind: str

    def sign(self, payload: str) -> str: ...


class HmacSigner:
    kind = "hmac"

    def __init__(self, secret: str) -> None:
        self._key = secret.encode("utf-8")

    def sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class Ed25519Signer:
    kind = "ed25519"

    def __init__(self, pem: bytes) -> None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        try:
            self._key = load_pem_private_key(pem, password=None)
        except TypeError as exc:
            raise ValueError("Ed25519 PEM must be unencrypted") from exc

    def sign(self, payload: str) -> str:
        raw = self._key.sign(payload.encode("utf-8"))
        return base64.b64encode(raw).decode("ascii")


def canonical_payload(params: dict) -> str:
    """Binance signs params sorted by name, joined as key=value with &."""
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


def verify_examples() -> None:
    """Check HMAC against the vectors published in the Binance API docs."""
    signer = HmacSigner("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    ascii_payload = (
        "apiKey=vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
        "&price=52000.00&quantity=0.01000000&recvWindow=100&side=SELL"
        "&symbol=BTCUSDT&timeInForce=GTC&timestamp=1645423376532&type=LIMIT"
    )
    unicode_payload = (
        "apiKey=vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
        "&price=0.10000000&quantity=1.00000000&recvWindow=5000&side=BUY"
        "&symbol=１２３４５６&timeInForce=GTC&timestamp=1645423376532&type=LIMIT"
    )
    ascii_sig = signer.sign(ascii_payload)
    unicode_sig = signer.sign(unicode_payload)
    if ascii_sig != "aa1b5712c094bc4e57c05a1a5c1fd8d88dcd628338ea863fec7b88e59fe2db24":
        raise AssertionError(ascii_sig)
    if unicode_sig != "b33892ae8e687c939f4468c6268ddd4c40ac1af18ad19a064864c47bae0752cd":
        raise AssertionError(unicode_sig)

    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "LIMIT",
        "timestamp": 1645423376532,
        "apiKey": "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A",
        "price": "52000.00",
        "quantity": "0.01000000",
        "recvWindow": 100,
        "timeInForce": "GTC",
    }
    if canonical_payload(params) != ascii_payload:
        raise AssertionError(canonical_payload(params))
