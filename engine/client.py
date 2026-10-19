"""Persistent Binance WebSocket client for spot and USD-M futures orders.

One TLS connection stays open. Orders are pipelined: the next frame can go
out before the previous ack comes back. Responses omit rate-limit blobs, and
order.place asks for ACK, the smallest order response that still carries orderId.
"""

from __future__ import annotations

import asyncio
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import orjson
from websockets.asyncio.client import connect

from engine.signer import Signer, canonical_payload

LIVE_URL = "wss://ws-api.binance.com:443/ws-api/v3?returnRateLimits=false"
TESTNET_URL = "wss://ws-api.testnet.binance.vision/ws-api/v3?returnRateLimits=false"
FUTURES_LIVE_URL = "wss://ws-fapi.binance.com/ws-fapi/v1"
FUTURES_TESTNET_URL = "wss://testnet.binancefuture.com/ws-fapi/v1"


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"Binance {status} code={code}: {message}")


@dataclass
class Pending:
    req_id: int
    future: asyncio.Future
    started_ns: int
    sent_ns: int


class OrderClient:
    def __init__(
        self,
        api_key: str = "",
        signer: Signer | None = None,
        *,
        testnet: bool = True,
        market: str = "spot",
        recv_window: int = 5000,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.signer = signer
        self.testnet = testnet
        self.market = "futures" if market == "futures" else "spot"
        self.recv_window = recv_window
        self.timeout = timeout
        if self.market == "futures":
            self.url = FUTURES_TESTNET_URL if testnet else FUTURES_LIVE_URL
        else:
            self.url = TESTNET_URL if testnet else LIVE_URL
        self.session_authed = False
        self._offset_ms = 0
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[Any, asyncio.Future] = {}
        self._next_id = 1
        self._send_lock = asyncio.Lock()

    async def __aenter__(self) -> OrderClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def clock_offset_ms(self) -> int:
        return self._offset_ms

    def timestamp_ms(self) -> int:
        return time.time_ns() // 1_000_000 + self._offset_ms

    async def connect(self) -> None:
        self._ws = await connect(
            self.url,
            compression=None,
            max_queue=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            max_size=1_048_576,
        )
        try:
            self._tune_socket()
            self._reader = asyncio.create_task(self._read_loop())
            await self.sync_time()
            if self.signer is not None and self.signer.kind == "ed25519":
                await self._logon()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()
        task = self._reader
        self._reader = None
        if task is not None:
            try:
                await task
            except Exception:
                pass
        self._fail_pending()

    def _tune_socket(self) -> None:
        transport = getattr(self._ws, "transport", None)
        if transport is None:
            return
        sock = transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                msg = orjson.loads(raw)
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        pending = self._pending
        self._pending = {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("websocket closed"))

    async def sync_time(self) -> int:
        started = time.time_ns()
        result = await self.call("time")
        finished = time.time_ns()
        local_mid_ms = (started + finished) // 2 // 1_000_000
        self._offset_ms = int(result["serverTime"]) - local_mid_ms
        return self._offset_ms

    async def _logon(self) -> None:
        if self.signer is None:
            raise RuntimeError("Ed25519 logon needs a private key")
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "timestamp": self.timestamp_ms(),
        }
        if self.recv_window:
            params["recvWindow"] = self.recv_window
        params["signature"] = self.signer.sign(canonical_payload(params))
        await self.call("session.logon", params, signed=False)
        self.session_authed = True

    def _auth_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.signer is None or not self.api_key:
            raise RuntimeError("Set BINANCE_API_KEY and a secret or Ed25519 PEM in .env")
        out = dict(params)
        out["timestamp"] = self.timestamp_ms()
        if self.recv_window:
            out["recvWindow"] = self.recv_window
        if not self.session_authed:
            out["apiKey"] = self.api_key
            out["signature"] = self.signer.sign(canonical_payload(out))
        return out

    async def submit(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Pending:
        if self._ws is None:
            raise RuntimeError("not connected")
        started_ns = time.perf_counter_ns()
        body = self._auth_params(params or {}) if signed else params
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": req_id, "method": method}
        if body:
            payload["params"] = body
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        raw = orjson.dumps(payload).decode()
        try:
            async with self._send_lock:
                await self._ws.send(raw)
        except Exception:
            self._pending.pop(req_id, None)
            raise
        return Pending(req_id, fut, started_ns, time.perf_counter_ns())

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Any:
        pending = await self.submit(method, params, signed=signed)
        try:
            msg = await asyncio.wait_for(asyncio.shield(pending.future), self.timeout)
        except TimeoutError:
            self._pending.pop(pending.req_id, None)
            if not pending.future.done():
                pending.future.cancel()
            raise TimeoutError(f"{method} timed out after {self.timeout:.0f}s") from None
        return unwrap(msg)

    async def last_price(self, symbol: str) -> str:
        result = await self.call("ticker.price", {"symbol": symbol})
        return str(result["price"])

    async def place_order(self, params: dict[str, Any]) -> dict:
        return await self.call("order.place", params, signed=True)

    async def cancel_order(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        if "orderId" not in params and "origClientOrderId" not in params:
            raise ValueError("cancel needs order_id or client_order_id")
        return await self.call("order.cancel", params, signed=True)

    def _futures_rest_sync(self, http_method: str, path: str, params: dict[str, Any]) -> Any:
        if self.signer is None:
            raise RuntimeError("Not connected")
        host = "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        signed_params: dict[str, Any] = dict(params)
        signed_params["recvWindow"] = self.recv_window
        signed_params["timestamp"] = self.timestamp_ms()
        qs = canonical_payload(signed_params)
        sig = self.signer.sign(qs)
        full_qs = f"{qs}&signature={sig}"
        if http_method == "POST":
            url = f"{host}{path}"
            data: bytes | None = full_qs.encode()
        else:
            url = f"{host}{path}?{full_qs}"
            data = None
        request = urllib.request.Request(
            url,
            data=data,
            method=http_method,
            headers={"X-MBX-APIKEY": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return orjson.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                raise BinanceError(exc.code, None, raw.decode(errors="replace")[:200]) from exc
            raise BinanceError(exc.code, payload.get("code"), str(payload.get("msg") or payload)) from exc

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await asyncio.to_thread(
            self._futures_rest_sync,
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": int(leverage)},
        )

    async def ensure_position_mode(self, hedge: bool) -> None:
        try:
            await asyncio.to_thread(
                self._futures_rest_sync,
                "POST",
                "/fapi/v1/positionSide/dual",
                {"dualSidePosition": "true" if hedge else "false"},
            )
        except BinanceError as exc:
            if exc.code == -4059:
                return
            raise

    async def cancel_all(self, symbol: str) -> list:
        if self.market == "futures":
            await asyncio.to_thread(
                self._futures_rest_sync,
                "DELETE",
                "/fapi/v1/allOpenOrders",
                {"symbol": symbol},
            )
            return []
        return await self.call("openOrders.cancelAll", {"symbol": symbol}, signed=True)

    async def open_orders(self, symbol: str) -> list:
        if self.market == "futures":
            result = await asyncio.to_thread(
                self._futures_rest_sync,
                "GET",
                "/fapi/v1/openOrders",
                {"symbol": symbol},
            )
            return result if isinstance(result, list) else []
        return await self.call("openOrders.status", {"symbol": symbol}, signed=True)

    async def open_positions(self) -> list:
        if self.market != "futures":
            return []
        result = await asyncio.to_thread(
            self._futures_rest_sync,
            "GET",
            "/fapi/v2/positionRisk",
            {},
        )
        if not isinstance(result, list):
            return []
        active = []
        for row in result:
            try:
                if Decimal(str(row.get("positionAmt") or "0")) != 0:
                    active.append(row)
            except Exception:
                continue
        return active

    async def test_order(self, params: dict[str, Any]) -> dict:
        return await self.call("order.test", params, signed=True)


def unwrap(msg: dict) -> Any:
    status = msg.get("status")
    if status != 200:
        err = msg.get("error") or {}
        raise BinanceError(int(status or 0), err.get("code"), err.get("msg", "request failed"))
    return msg.get("result")
