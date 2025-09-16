from ib_async import *
import pandas as pd
import asyncio
import os
import random
from pathlib import Path

from dbactivator import (
    record_5s_bar,
    insert_fill_for_order,
    fetch_signals_new,
    mark_signal_status,
)

# ---- Globals ----
# Default output location; can be overridden by CLI arg 2
TRADES_DB_PATH = Path("./data/trades.db")
SIGNALS_ORDERS_DB_PATH = Path("./data/signals_orders.db")
PNL_DB_PATH = Path("./data/pnl.db")


# No CSV; write minute bars directly to trades.db via dbactivator

# Define a new place_adaptive_order asynchronous function
# The function will be called from the watch_signals loop when a signal is detected

async def place_adaptive_order(ib_client: IB, contract: Future, side: str, quantity: int, symbol: str, adaptive_priority: str = 'Normal') -> None:
    """
    Places an adaptive market order.

    Args:
        ib_client: The connected IB client instance.
        contract: The contract for the order.
        side: 'BUY' or 'SELL'.
        quantity: The number of shares/contracts.
        adaptive_priority: 'Urgent', 'Normal', or 'Patient'.
    """
    try:
        # For simplicity, using a MarketOrder with Adaptive.
        # If you need a LimitOrder, you'd also pass `price` here.
        order = MarketOrder(side, quantity)
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', adaptive_priority)]

        print(f"Attempting to place adaptive {side} order for {quantity} {contract.symbol} with priority '{adaptive_priority}'...", flush=True)

        trade = await ib_client.placeOrderAsync(contract, order) # Use placeOrderAsync for non-blocking
        print(f"Order {trade.order.permId} submitted.", flush=True)

        # You can add monitoring here or rely on your existing event handlers if needed
        await ib_client.waitOnUpdate(trade.isDone) # Or use a timeout if you don't want to block
        if trade.isDone:
            print(f"Order {trade.order.permId} is {trade.orderStatus.status}.", flush=True)
            for fill in trade.fills:
                print(f"  - Filled {fill.execution.shares} at {fill.execution.avgPrice}", flush=True)
                # Persist fill to DB
                await insert_fill_for_order(
                    order_id=int(trade.order.permId),
                    exec_id=str(fill.execution.execId),
                    ts_utc=(fill.execution.time or ""),
                    side=(fill.execution.side or ""),
                    qty=float(fill.execution.shares or 0),
                    price=float(fill.execution.avgPrice or 0),
                    symbol=symbol,
                )
        else:
            print(f"Order {trade.order.permId} status: {trade.orderStatus.status}", flush=True)

    except Exception as e:
        print(f"Error placing adaptive order: {e!r}", flush=True)


def _client_id_for_symbol(symbol: str) -> int:
    try:
        base = int(os.getenv("CLIENT_ID", "17") or 17)
    except Exception:
        base = 17
    # Stable offset per symbol to avoid collisions; keep small
    offset = abs(hash(symbol)) % 900 + 1
    return base * 1000 + offset


async def streamdata(symbol: str, exchange: str | None = None) -> None:
    ib = IB()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "7497") or 7497)
    client_id = _client_id_for_symbol(symbol)
    await ib.connectAsync(host, port, clientId=client_id)

    # Contract config (single line, no qualify)
    exch = (exchange or os.getenv("EXCHANGE", "CME") or "CME").strip()
    contract = Future(symbol=symbol, lastTradeDateOrContractMonth=os.getenv('EXPIRY', '202509'), exchange=exch, currency='USD')
    print(contract, flush=True)

    # 5 seconds is the only supported size for real-time bars
    # Set useRTH=False to receive bars outside regular hours (Globex)
    bars = ib.reqRealTimeBars(contract, 5, 'TRADES', False)

    def on_bar_update(bar_list, hasNewBar):
        if not hasNewBar or not bar_list:
            return

        b = bar_list[-1]
        print(b, flush=True)

        # Persist 5s bar and update minute snapshot
        ts5 = pd.Timestamp(b.time)
        asyncio.create_task(
            record_5s_bar(
                symbol,
                ts5,
                float(b.open_),
                float(b.high),
                float(b.low),
                float(b.close),
                float(getattr(b, 'volume', 0) or 0),
                float(getattr(b, 'wap', 0) or 0),
                int(getattr(b, 'count', 0) or 0),
            )
        )

    bars.updateEvent += on_bar_update

    # NEW: watch for signals in DB (runs independently)
    async def watch_signals(symbol: str):
        last_seen = None
        while True:
            sigs = await fetch_signals_new(symbol)
            if sigs is not None and not sigs.empty:
                for ts, row in sigs.iterrows():
                    side = (row.get('side') or 'BUY')
                    qty = int(row.get('qty') or 1)
                    ap  = row.get('adaptive_priority') or 'Normal'
                    price = row.get('price')
                    print(f"signal {symbol} {ts}: {side} @ {price}", flush=True)
                    # Place order concurrently
                    asyncio.create_task(
                        place_adaptive_order(ib, contract, side, qty, symbol, adaptive_priority=ap)
                    )
                    # Mark as SENT
                    await mark_signal_status(symbol, ts, row.get('strategy', 'sma'), 'SENT')
            await asyncio.sleep(1.0)

    watcher = None
    if os.getenv("ORDER_IN_STREAMER", "false").lower() == "true":
        watcher = asyncio.create_task(watch_signals(symbol))

    # Keep the task alive so 5s bars can print; Ctrl+C to exit
    print("Streaming 5s bars...", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        # stop background tasks first so they can exit
        for task in (watcher,):
            try:
                if task and not task.done():
                    task.cancel()
            except Exception:
                pass
        ib.cancelRealTimeBars(bars)
        ib.disconnect()

async def run_with_reconnect(symbol: str, exchange: str | None = None) -> None:
    delay = 1.0
    max_delay = 30.0
    while True:
        try:
            await streamdata(symbol, exchange)
            delay = 1.0  # reset after a clean run
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"stream error: {e!r}", flush=True)
            jitter = random.uniform(0, delay / 2)
            sleep_for = min(delay + jitter, max_delay)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else 'MES'
    exch = sys.argv[2] if len(sys.argv) > 2 else os.getenv("EXCHANGE", "CME")
    asyncio.run(run_with_reconnect(sym, exch))
