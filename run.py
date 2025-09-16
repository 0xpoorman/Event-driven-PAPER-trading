import asyncio
import os
from dotenv import load_dotenv

from ib_async import IB
from strategy import SMACrossoverStrategy
from dbactivator import snapshot

from brokers.ib.streamer_ib import run_with_reconnect
from brokers.ib.order_router_ib import OrderRouter


def _parse_symbols(default_exchange: str) -> list[tuple[str, str]]:
    symbols_env = os.getenv("SYMBOLS", "MES,MNQ,MCL")
    pairs: list[tuple[str, str]] = []
    for raw in [s.strip() for s in symbols_env.split(",") if s.strip()]:
        if ":" in raw:
            symbol, exch = raw.split(":", 1)
            pairs.append((symbol.strip(), exch.strip() or default_exchange))
        else:
            pairs.append((raw, default_exchange))
    return pairs


async def main():
    load_dotenv()
    default_exchange = os.getenv("EXCHANGE", "CME")
    expiry = os.getenv("EXPIRY", "202509")
    sym_pairs = _parse_symbols(default_exchange)

    # Start IB market data streamers per symbol
    stream_tasks = [
        asyncio.create_task(run_with_reconnect(symbol, exchange))
        for symbol, exchange in sym_pairs
    ]

    # SMA strategy works off the shared minute snapshot and writes signals
    dummy_ib = IB()
    fast = int(os.getenv("FAST_MA", "5") or 5)
    slow = int(os.getenv("SLOW_MA", "12") or 12)
    qty = int(os.getenv("QTY", "1") or 1)
    strategy = SMACrossoverStrategy(dummy_ib, sym_pairs, expiry, fast=fast, slow=slow, qty=qty)
    tasks = stream_tasks + [asyncio.create_task(strategy.run(snapshot))]

    # Optional IB order router
    if os.getenv("START_ROUTER", "true").lower() == "true":
        host = os.getenv("HOST", "127.0.0.1")
        port = int(os.getenv("PORT", "7497") or 7497)
        router_ib = IB()
        await router_ib.connectAsync(host, port, clientId=97)
        symbol_exchange_map = {symbol: exchange for symbol, exchange in sym_pairs}
        router = OrderRouter(router_ib, expiry=expiry, exchange=default_exchange, symbol_exchanges=symbol_exchange_map)
        tasks.append(asyncio.create_task(router.run()))

    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in done:
        exc = task.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
