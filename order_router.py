import os
import asyncio
from typing import Dict, Optional

from ib_async import IB, Future, MarketOrder, TagValue
from dbactivator import fetch_signals_new_all, mark_signal_status


class OrderRouter:
    """Polls NEW signals and places orders via IB.

    Simple router for demo/prod-lite. Contracts are cached by symbol and
    constructed as Futures using EXPIRY/EXCHANGE envs.
    """

    def __init__(self, ib: IB, expiry: Optional[str] = None, exchange: Optional[str] = None, symbol_exchanges: Optional[Dict[str, str]] = None):
        self.ib = ib
        self.expiry = expiry or os.getenv("EXPIRY", "202509")
        self.exchange = exchange or os.getenv("EXCHANGE", "CME")
        self._contracts: Dict[str, Future] = {}
        # optional per-symbol exchange mapping, e.g., {"MCL": "NYMEX"}
        self.symbol_exchanges = symbol_exchanges or {}

    async def _get_contract(self, symbol: str) -> Future:
        c = self._contracts.get(symbol)
        if c is None:
            exch = self.symbol_exchanges.get(symbol, self.exchange)
            c = Future(symbol=symbol, lastTradeDateOrContractMonth=self.expiry, exchange=exch, currency="USD")
            try:
                await self.ib.qualifyContractsAsync(c)
            except Exception:
                pass
            self._contracts[symbol] = c
        return c

    async def run(self, poll_interval: float = 1.0):
        while True:
            try:
                df = await fetch_signals_new_all()
                if df is not None and not df.empty:
                    for ts, row in df.iterrows():
                        symbol = row.get("symbol")
                        side = (row.get("side") or "BUY").upper()
                        qty = int(row.get("qty") or 1)
                        ap = row.get("adaptive_priority") or "Normal"
                        strat = row.get("strategy", "sma")

                        c = await self._get_contract(symbol)
                        order = MarketOrder(side, qty)
                        order.algoStrategy = "Adaptive"
                        order.algoParams = [TagValue("adaptivePriority", ap)]
                        try:
                            await self.ib.placeOrderAsync(c, order)
                            await mark_signal_status(symbol, ts, strat, "SENT")
                        except Exception as e:
                            print(f"order place error for {symbol} {side} x{qty}: {e!r}", flush=True)
            except Exception as e:
                print(f"router poll error: {e!r}", flush=True)

            await asyncio.sleep(poll_interval)
