# # --- strategy.py (simple, event-driven) ---
# import asyncio
# from dataclasses import dataclass, field
# from typing import Callable, Awaitable

# from indicators import RollingSMA, Bollinger
# from dbactivator import record_signal_async  # your existing function

# @dataclass
# class SMACrossBBands:
#     name: str
#     symbol: str
#     fast: int = 9
#     slow: int = 21
#     bb_window: int = 20
#     bb_k: float = 2.0

#     # internal rolling state
#     sma_fast: RollingSMA = field(init=False)
#     sma_slow: RollingSMA = field(init=False)
#     bb: Bollinger = field(init=False)
#     last_cross_side: str | None = field(default=None, init=False)  # "BUY"/"SELL"

#     def __post_init__(self):
#         self.sma_fast = RollingSMA(self.fast)
#         self.sma_slow = RollingSMA(self.slow)
#         self.bb = Bollinger(self.bb_window, self.bb_k)

#     async def on_bar(self, ts_utc: str, close: float) -> None:
#         f = self.sma_fast.push(close)
#         s = self.sma_slow.push(close)
#         bands = self.bb.push(close)  # dict or None

#         if f is None or s is None:
#             return  # not ready yet

#         # Cross logic kept super simple:
#         spread_prev = None  # you can store last values if you want prev/curr test
#         spread = f - s

#         side: str | None = None
#         # Basic: zero-cross on spread
#         if self.last_cross_side is None:
#             # initialize state only when first complete info present
#             self.last_cross_side = "BUY" if spread > 0 else "SELL"
#             return
#         else:
#             # change when the sign flips
#             new_side = "BUY" if spread > 0 else "SELL"
#             if new_side != self.last_cross_side:
#                 side = new_side
#                 self.last_cross_side = new_side

#         # (Optional) filter with Bollinger: only act if close pierces band
#         if side and bands:
#             if side == "BUY" and close < bands["lower"]:
#                 # mean-reversion buy
#                 await record_signal_async(
#                     symbol=self.symbol,
#                     ts_utc=ts_utc,
#                     action="BUY",
#                     qty=1.0,
#                     price=close,
#                     strategy=self.name,
#                     meta={"reason": "sma_cross & below_lower_band"}
#                 )
#             elif side == "SELL" and close > bands["upper"]:
#                 # mean-reversion sell
#                 await record_signal_async(
#                     symbol=self.symbol,
#                     ts_utc=ts_utc,
#                     action="SELL",
#                     qty=1.0,
#                     price=close,
#                     strategy=self.name,
#                     meta={"reason": "sma_cross & above_upper_band"}
#                 )
#         # If you want pure SMA cross only, just drop the band checks and write immediately.



import asyncio
import pandas as pd
from dataclasses import dataclass
from typing import Callable

from dbactivator import record_signal_async, fetch_signals_new, mark_signal_status
from ib_async import IB, Future, MarketOrder, TagValue


@dataclass
class SMACrossoverStrategy:
    ib: IB
    symbols: list[tuple[str, str]]  # list of (symbol, exchange)
    expiry: str
    fast: int = 9
    slow: int = 21
    qty: int = 1
    strategy_name: str = "sma"

    async def run(self, snapshot_func: Callable[[str], pd.DataFrame]):
        last_sig: dict[str, str] = {}  # symbol -> last side
        while True:
            try:
                for sym, exch in self.symbols:
                    df = snapshot_func(sym)
                    if df is None or df.empty or len(df) < self.slow + 2:
                        continue
                    s = df["close"].astype(float)
                    fast = s.rolling(self.fast).mean()
                    slow = s.rolling(self.slow).mean()
                    sig = (fast - slow)
                    # check latest two points for cross
                    if pd.isna(sig.iloc[-1]) or pd.isna(sig.iloc[-2]):
                        continue
                    prev, curr = sig.iloc[-2], sig.iloc[-1]
                    ts = df.index[-1]
                    side: str | None = None
                    if prev <= 0 and curr > 0:
                        side = "BUY"
                    elif prev >= 0 and curr < 0:
                        side = "SELL"
                    if side and last_sig.get(sym) != side:
                        price = float(df["close"].iloc[-1])
                        print(f"strategy signal {sym} {ts}: {side} @ {price}", flush=True)
                        await record_signal_async(
                            symbol=sym,
                            ts_utc=ts,
                            action=side,
                            qty=float(self.qty),
                            price=price,
                            strategy=self.strategy_name,
                            order_type="MKT",
                            adaptive_priority="Normal",
                            status="NEW",
                        )
                        last_sig[sym] = side
            except Exception as e:
                print(f"strategy loop error: {e!r}", flush=True)
            await asyncio.sleep(1)


async def watch_signals(ib: IB, contract: Future, symbol: str):
    while True:
        try:
            df = await fetch_signals_new(symbol)
            if df is not None and not df.empty:
                for ts, row in df.iterrows():
                    side = (row.get('side') or 'BUY')
                    qty = int(row.get('qty') or 1)
                    ap  = row.get('adaptive_priority') or 'Normal'
                    print(f"signal {symbol} {ts}: {side}", flush=True)
                    order = MarketOrder(side, qty)
                    order.algoStrategy = 'Adaptive'
                    order.algoParams = [TagValue('adaptivePriority', ap)]
                    try:
                        await ib.placeOrderAsync(contract, order)
                        await mark_signal_status(symbol, ts, row.get('strategy','sma'), 'SENT')
                    except Exception as e:
                        print(f"order place error: {e!r}", flush=True)
        except Exception as e:
            print(f"watch_signals error: {e!r}", flush=True)
        await asyncio.sleep(1)
