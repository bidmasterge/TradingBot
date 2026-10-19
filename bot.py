"""Place and cancel Binance spot orders over a persistent WebSocket.

Testnet is the default. Real-money commands also need --live.

  py -3 bot.py self-check
  py -3 bot.py ping
  py -3 bot.py test --symbol BTCUSDT --side BUY --qty 0.001 --price 10000
  py -3 bot.py place --symbol BTCUSDT --side BUY --qty 0.001 --price 10000
  py -3 bot.py cancel --symbol BTCUSDT --order-id 123456
  py -3 bot.py flash --symbol BTCUSDT --qty 0.001 --price 10000
  py -3 bot.py bench --symbol BTCUSDT --qty 0.001 --price 10000 --count 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

import orjson
from dotenv import load_dotenv

from engine.client import LIVE_URL, TESTNET_URL, BinanceError, OrderClient, unwrap
from engine.signer import Ed25519Signer, HmacSigner, Signer, verify_examples

ROOT = Path(__file__).resolve().parent
DECIMAL = re.compile(r"^\d+(\.\d+)?$")


def settings() -> tuple[str, Signer | None, bool, int]:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    secret = os.getenv("BINANCE_API_SECRET", "").strip()
    pem_path = os.getenv("BINANCE_PRIVATE_KEY_PATH", "").strip()
    testnet = os.getenv("BINANCE_TESTNET", "true").strip().lower() in {"1", "true", "yes", "y"}
    recv_window = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))
    signer: Signer | None = None
    if pem_path:
        signer = Ed25519Signer(Path(pem_path).read_bytes())
    elif secret:
        signer = HmacSigner(secret)
    return api_key, signer, testnet, recv_window


def guard_live(args: argparse.Namespace, testnet: bool) -> None:
    if testnet or getattr(args, "live", False):
        return
    raise SystemExit("Refusing live orders. Add --live only when you mean to trade real funds.")


def check_decimal(name: str, value: str | None) -> None:
    if value is not None and not DECIMAL.fullmatch(value):
        raise SystemExit(f"{name} must be a plain decimal like 0.001")


def order_params(args: argparse.Namespace) -> dict:
    check_decimal("qty", getattr(args, "qty", None))
    check_decimal("quote-qty", getattr(args, "quote_qty", None))
    check_decimal("price", getattr(args, "price", None))
    order_type = args.type.upper()
    params: dict = {
        "symbol": args.symbol.upper(),
        "side": args.side.upper(),
        "type": order_type,
        "newOrderRespType": args.resp.upper(),
    }
    if args.qty:
        params["quantity"] = args.qty
    if args.quote_qty:
        params["quoteOrderQty"] = args.quote_qty
    if args.price:
        params["price"] = args.price
    if order_type == "LIMIT":
        params["timeInForce"] = args.tif.upper()
    if args.client_id:
        params["newClientOrderId"] = args.client_id
    return params


def dump(result: object) -> None:
    print(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())


def ms(ns: int) -> str:
    return f"{ns / 1_000_000:.2f} ms"


async def _client() -> OrderClient:
    api_key, signer, testnet, recv_window = settings()
    return OrderClient(api_key, signer, testnet=testnet, recv_window=recv_window)


def _say_connected(client: OrderClient) -> None:
    where = "TESTNET" if client.testnet else "LIVE"
    auth = "ed25519 session" if client.session_authed else (client.signer.kind if client.signer else "public")
    print(f"connected {where}  auth={auth}  clock_offset={client.clock_offset_ms} ms", file=sys.stderr)
    if not client.testnet:
        print("LIVE TRADING — orders use real funds", file=sys.stderr)


async def cmd_ping(_args: argparse.Namespace) -> None:
    async with await _client() as client:
        _say_connected(client)
        pending = await client.submit("time")
        msg = await pending.future
        unwrap(msg)
        ack = time.perf_counter_ns()
        print(f"time round trip {ms(ack - pending.sent_ns)}  url={client.url}")


async def cmd_rtt(args: argparse.Namespace) -> None:
    samples: list[int] = []
    async with await _client() as client:
        _say_connected(client)
        for _ in range(args.count):
            pending = await client.submit("time")
            unwrap(await pending.future)
            samples.append(time.perf_counter_ns() - pending.sent_ns)
    print(summarize("public time", samples))


async def cmd_test(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    async with await _client() as client:
        _say_connected(client)
        dump(await client.test_order(order_params(args)))
        print("signature accepted, no order was sent", file=sys.stderr)


async def cmd_place(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    async with await _client() as client:
        _say_connected(client)
        pending = await client.submit("order.place", order_params(args), signed=True)
        msg = unwrap(await pending.future)
        ack = time.perf_counter_ns()
        dump(msg)
        print(
            f"place local {ms(pending.sent_ns - pending.started_ns)}  "
            f"network {ms(ack - pending.sent_ns)}",
            file=sys.stderr,
        )


async def cmd_cancel(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    if args.order_id is None and not args.client_id:
        raise SystemExit("pass --order-id (faster) or --client-id")
    async with await _client() as client:
        _say_connected(client)
        pending = await client.submit(
            "order.cancel",
            _cancel_params(args.symbol, args.order_id, args.client_id),
            signed=True,
        )
        msg = unwrap(await pending.future)
        ack = time.perf_counter_ns()
        dump(msg)
        print(
            f"cancel local {ms(pending.sent_ns - pending.started_ns)}  "
            f"network {ms(ack - pending.sent_ns)}",
            file=sys.stderr,
        )


async def cmd_cancel_all(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    async with await _client() as client:
        _say_connected(client)
        dump(await client.cancel_all(args.symbol.upper()))


async def cmd_open(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    async with await _client() as client:
        _say_connected(client)
        dump(await client.open_orders(args.symbol.upper()))


async def cmd_flash(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    async with await _client() as client:
        _say_connected(client)
        await _assert_resting(client, args)
        report = await _flash_once(client, args)
        _print_flash(report)


async def cmd_bench(args: argparse.Namespace) -> None:
    _api, _signer, testnet, _recv = settings()
    guard_live(args, testnet)
    if args.count < 1 or args.count > 20:
        raise SystemExit("--count must be from 1 to 20")
    place_net: list[int] = []
    cancel_net: list[int] = []
    async with await _client() as client:
        _say_connected(client)
        await _assert_resting(client, args)
        await _flash_once(client, args)
        for i in range(args.count):
            report = await _flash_once(client, args)
            place_net.append(report["place_net"])
            cancel_net.append(report["cancel_net"])
            print(
                f"{i + 1}  place {ms(report['place_net'])}  cancel {ms(report['cancel_net'])}  "
                f"orderId {report['order_id']}",
                file=sys.stderr,
            )
    print(summarize("place network", place_net))
    print(summarize("cancel network", cancel_net))


async def _assert_resting(client: OrderClient, args: argparse.Namespace) -> None:
    """Refuse a flash price that could fill. Flash is a latency check, not a take."""
    check_decimal("qty", args.qty)
    check_decimal("price", args.price)
    last = Decimal(await client.last_price(args.symbol.upper()))
    limit = Decimal(args.price)
    side = args.side.upper()
    if side == "BUY" and limit >= last * Decimal("0.99"):
        raise SystemExit(
            f"Refusing BUY at {limit}. Last price is {last}. Use a price at least 1% below the market."
        )
    if side == "SELL" and limit <= last * Decimal("1.01"):
        raise SystemExit(
            f"Refusing SELL at {limit}. Last price is {last}. Use a price at least 1% above the market."
        )
    print(f"last {last}  resting {side} {limit}", file=sys.stderr)


async def _flash_once(client: OrderClient, args: argparse.Namespace) -> dict:
    symbol = args.symbol.upper()
    client_id = f"f{time.time_ns():x}"[-36:]
    place_params = {
        "symbol": symbol,
        "side": args.side.upper(),
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": args.qty,
        "price": args.price,
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
    }
    # Place frame first, then cancel by client id, without waiting for the ack.
    # Binance matches cancel-by-orderId faster, so if the early cancel loses
    # the race we immediately cancel again with the orderId from the ack.
    place_pending = await client.submit("order.place", place_params, signed=True)
    cancel_pending = await client.submit(
        "order.cancel",
        {"symbol": symbol, "origClientOrderId": client_id},
        signed=True,
    )
    place_msg = await place_pending.future
    place_ack = time.perf_counter_ns()
    cancel_msg = await cancel_pending.future
    cancel_ack = time.perf_counter_ns()
    place_result = unwrap(place_msg)
    order_id = place_result["orderId"]
    cancel_sent = cancel_pending.sent_ns
    cancel_started = cancel_pending.started_ns
    try:
        try:
            cancel_result = unwrap(cancel_msg)
        except BinanceError as exc:
            if exc.code != -2011:
                raise
            retry = await client.submit(
                "order.cancel",
                {"symbol": symbol, "orderId": order_id},
                signed=True,
            )
            cancel_result = unwrap(await retry.future)
            cancel_ack = time.perf_counter_ns()
            cancel_sent = retry.sent_ns
            cancel_started = retry.started_ns
    except Exception:
        print(f"cancel failed, orderId {order_id} may still be open", file=sys.stderr)
        raise
    return {
        "order_id": place_result.get("orderId"),
        "place_local": place_pending.sent_ns - place_pending.started_ns,
        "place_net": place_ack - place_pending.sent_ns,
        "cancel_local": cancel_sent - cancel_started,
        "cancel_net": cancel_ack - cancel_sent,
        "status": cancel_result.get("status"),
    }


def _print_flash(report: dict) -> None:
    print(
        f"place  local {ms(report['place_local'])}  network {ms(report['place_net'])}  "
        f"orderId {report['order_id']}"
    )
    print(
        f"cancel local {ms(report['cancel_local'])}  network {ms(report['cancel_net'])}  "
        f"status {report['status']}"
    )


def _cancel_params(symbol: str, order_id: int | None, client_id: str | None) -> dict:
    params: dict = {"symbol": symbol.upper()}
    if order_id is not None:
        params["orderId"] = order_id
    if client_id:
        params["origClientOrderId"] = client_id
    return params


def summarize(label: str, samples: list[int]) -> str:
    ordered = sorted(samples)
    n = len(ordered)
    avg = sum(ordered) // n

    def pct(p: float) -> int:
        return ordered[min(n - 1, int(p * (n - 1)))]

    return (
        f"{label}  n={n}  min {ms(ordered[0])}  med {ms(pct(0.5))}  "
        f"avg {ms(avg)}  p95 {ms(pct(0.95))}  max {ms(ordered[-1])}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fast Binance spot place/cancel bot (WebSocket API).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Testnet keys: https://testnet.binance.vision\n"
            "Copy .env.example to .env before trading commands.\n"
            f"Live socket: {LIVE_URL}\n"
            f"Testnet socket: {TESTNET_URL}"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--live", action="store_true", help="Required for real-money orders.")

    order = argparse.ArgumentParser(add_help=False)
    order.add_argument("--symbol", default="BTCUSDT")
    order.add_argument("--side", default="BUY", choices=["BUY", "SELL", "buy", "sell"])
    order.add_argument("--type", default="LIMIT", choices=["LIMIT", "MARKET", "limit", "market"])
    order.add_argument("--qty", help="Base quantity, for example 0.001")
    order.add_argument("--quote-qty", help="Quote quantity for MARKET orders")
    order.add_argument("--price", help="Limit price")
    order.add_argument("--tif", default="GTC", help="Time in force for LIMIT orders")
    order.add_argument("--client-id", help="newClientOrderId, 1-36 chars")
    order.add_argument("--resp", default="ACK", choices=["ACK", "RESULT", "FULL", "ack", "result", "full"])

    sub.add_parser("self-check", help="Verify local HMAC signing against Binance's published example.")
    sub.add_parser("ping", help="Connect and measure one public time round trip.")

    rtt = sub.add_parser("rtt", help="Measure public WebSocket round trip.")
    rtt.add_argument("--count", type=int, default=10)

    sub.add_parser("test", parents=[live, order], help="Validate a signed order without sending it.")
    sub.add_parser("place", parents=[live, order], help="Place an order. Default response is ACK.")

    cancel = sub.add_parser("cancel", parents=[live], help="Cancel one order. --order-id is faster.")
    cancel.add_argument("--symbol", default="BTCUSDT")
    cancel.add_argument("--order-id", type=int)
    cancel.add_argument("--client-id")

    cancel_all = sub.add_parser("cancel-all", parents=[live], help="Cancel every open order on a symbol.")
    cancel_all.add_argument("--symbol", default="BTCUSDT")

    opened = sub.add_parser("open", parents=[live], help="List open orders on a symbol.")
    opened.add_argument("--symbol", default="BTCUSDT")

    flash = sub.add_parser(
        "flash",
        parents=[live],
        help="Place a resting limit and cancel it immediately. Price must be away from the market.",
    )
    _add_resting(flash)
    bench = sub.add_parser(
        "bench",
        parents=[live],
        help="Repeat flash and print place/cancel latency. Count is capped at 20.",
    )
    _add_resting(bench)
    bench.add_argument("--count", type=int, default=5)

    return parser


def _add_resting(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL", "buy", "sell"])
    parser.add_argument("--qty", required=True)
    parser.add_argument("--price", required=True)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "self-check":
        verify_examples()
        print("HMAC examples match Binance's published signatures")
        return
    commands = {
        "ping": cmd_ping,
        "rtt": cmd_rtt,
        "test": cmd_test,
        "place": cmd_place,
        "cancel": cmd_cancel,
        "cancel-all": cmd_cancel_all,
        "open": cmd_open,
        "flash": cmd_flash,
        "bench": cmd_bench,
    }
    try:
        asyncio.run(commands[args.cmd](args))
    except BinanceError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
