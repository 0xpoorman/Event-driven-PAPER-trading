from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Dict
import threading
import asyncio

import pandas as pd

# Paths match sqlitedb_setup.py
TRADES_DB_PATH = Path("./data/trades.db")
SIGNALS_ORDERS_DB_PATH = Path("./data/signals_orders.db")
PNL_DB_PATH = Path("./data/pnl.db")
FILLS_CSV_PATH = Path("./data/fills.csv")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA journal_mode = WAL;")
    con.execute("PRAGMA synchronous = NORMAL;")
    con.execute("PRAGMA busy_timeout = 3000;")
    return con


def _append_fill_csv(fill_id: str, order_id: int, symbol: str, ts_utc: str, side: str, qty: float, price: float) -> None:
    try:
        FILLS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_file = not FILLS_CSV_PATH.exists()
        with FILLS_CSV_PATH.open('a', newline='') as f:
            import csv
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["fill_id","order_id","symbol","ts_utc","side","qty","price"])
            writer.writerow([fill_id, int(order_id), symbol, ts_utc, side, float(qty), float(price)])
    except Exception:
        # Non-fatal; CSV used only for optional PnL path
        pass


# ---------------- In-memory minute bars (replaces state.py) -----------------
_store: Dict[str, pd.DataFrame] = {}
_lock = threading.Lock()
# Cap in-memory history length (minutes). Default 120; override via env MAX_MINUTES
MAX_MINUTES = int(os.getenv("MAX_MINUTES", "120") or 120)
_last_5s_close: Dict[str, float] = {}


def snapshot(symbol: str) -> pd.DataFrame:
    with _lock:
        return _store.get(symbol, pd.DataFrame()).copy()


def _upsert_minute_in_memory(symbol: str, ts_minute_utc, o, h, l, c, v) -> None:
    global _store
    with _lock:
        df = _store.get(symbol)
        idx = pd.Timestamp(ts_minute_utc)
        if idx.tzinfo is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        vol_up = 0.0
        vol_down = 0.0
        prev = _last_5s_close.get(symbol)
        if prev is not None:
            if float(c) >= float(prev):
                vol_up = float(v)
            else:
                vol_down = float(v)
        _last_5s_close[symbol] = float(c)

        if df is None or df.empty:
            df = pd.DataFrame(
                [(o, h, l, c, v, vol_up, vol_down)],
                index=pd.DatetimeIndex([idx]),
                columns=["open","high","low","close","volume","vol_up","vol_down"],
            )
        else:
            if idx in df.index:
                cur = df.loc[idx]
                df.loc[idx, "open"] = cur["open"] if pd.notna(cur["open"]) else o
                df.loc[idx, "high"] = max(cur["high"], h)
                df.loc[idx, "low"]  = min(cur["low"],  l)
                df.loc[idx, "close"] = c
                df.loc[idx, "volume"] = float(cur.get("volume", 0) or 0) + float(v or 0)
                df.loc[idx, "vol_up"] = float(cur.get("vol_up", 0) or 0) + vol_up
                df.loc[idx, "vol_down"] = float(cur.get("vol_down", 0) or 0) + vol_down
            else:
                df.loc[idx] = [o, h, l, c, v, vol_up, vol_down]
            if len(df) > MAX_MINUTES:
                df = df.iloc[-MAX_MINUTES:]
        _store[symbol] = df.sort_index()


# --------------------------- Trades: OHLCV 1m -------------------------------
def flush_minute_bars(symbol: str, df_1m: pd.DataFrame) -> int:
    """Idempotent upsert of 1‑minute bars to SQLite trades.db.

    Expects `df_1m` indexed by minute (UTC) with at least open,high,low,close,volume.
    Populates required extras: wap, count, vol_up, vol_down, position.
    """
    if df_1m is None or df_1m.empty:
        return 0

    df = df_1m.copy()
    # Ensure index is UTC-aware and ISO strings
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    df["ts_utc"] = idx.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Required columns with sensible fallbacks
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = 0.0 if col != "volume" else 0

    if "wap" not in df.columns:
        df["wap"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    if "count" not in df.columns:
        df["count"] = 0
    # Respect provided per-minute split if present; otherwise derive a simple split
    if "vol_up" not in df.columns or "vol_down" not in df.columns:
        up_mask = (df["close"] >= df["open"]).astype(bool)
        df["vol_up"] = df["volume"].where(up_mask, 0)
        df["vol_down"] = df["volume"].where(~up_mask, 0)
    if "position" not in df.columns:
        df["position"] = 0.0

    cols = [
        "symbol",
        "ts_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "wap",
        "count",
        "vol_up",
        "vol_down",
        "position",
    ]
    df["symbol"] = symbol
    rows = df[cols].to_records(index=False)

    sql = (
        "INSERT INTO ohlcv_1m (symbol, ts_utc, open, high, low, close, volume, wap, count, vol_up, vol_down, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol, ts_utc) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, wap=excluded.wap, count=excluded.count, "
        "vol_up=excluded.vol_up, vol_down=excluded.vol_down, position=excluded.position"
    )
    con = _connect(TRADES_DB_PATH)
    try:
        con.executemany(sql, rows)
        con.commit()
        return len(rows)
    finally:
        con.close()


async def upsert_minute_bar(symbol: str, ts_minute_utc, o, h, l, c, v) -> None:
    """Update in-memory minute bar and persist that minute to trades.db."""
    _upsert_minute_in_memory(symbol, ts_minute_utc, o, h, l, c, v)
    # create a one-row df to persist
    ts = pd.Timestamp(ts_minute_utc)
    if getattr(ts, 'tz', None) is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    # Use the aggregated in-memory minute after the update
    mem = snapshot(symbol)
    if ts not in mem.index:
        # fallback to provided values if not found
        df = pd.DataFrame(
            {
                "open": [float(o)],
                "high": [float(h)],
                "low": [float(l)],
                "close": [float(c)],
                "volume": [float(v)],
                "vol_up": [float(v) if float(c) >= float(o) else 0.0],
                "vol_down": [float(v) if float(c) < float(o) else 0.0],
            },
            index=pd.DatetimeIndex([ts]),
        )
    else:
        r = mem.loc[ts]
        df = pd.DataFrame(
            {
                "open": [float(r["open"])],
                "high": [float(r["high"])],
                "low": [float(r["low"])],
                "close": [float(r["close"])],
                "volume": [float(r.get("volume", 0))],
                "vol_up": [float(r.get("vol_up", 0))],
                "vol_down": [float(r.get("vol_down", 0))],
            },
            index=pd.DatetimeIndex([ts]),
        )
    # offload sync DB write to a thread
    await asyncio.to_thread(flush_minute_bars, symbol, df)


# ------------------------- Signals: orders intent ----------------------------
def record_signal(
    symbol: str,
    ts_utc,
    action: str,
    qty: float,
    price: float,
    strategy: str = "sma",
    order_type: str = "MKT",
    limit_price: Optional[float] = None,
    adaptive_priority: Optional[str] = None,
    status: str = "NEW",
) -> int:
    """Upsert a signal into signals_orders.db signals table.

    Returns the affected row count (1).
    """
    ts = pd.Timestamp(ts_utc)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    action = action.upper()
    if action not in ("BUY", "SELL"):
        raise ValueError("action must be 'BUY' or 'SELL'")
    order_type = order_type.upper()
    if order_type not in ("MKT", "LMT", "STP", "STP_LMT"):
        raise ValueError("order_type must be one of MKT,LMT,STP,STP_LMT")

    sql = (
        "INSERT INTO signals(symbol, ts_utc, action, qty, price, strategy, order_type, limit_price, adaptive_priority, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol, ts_utc, strategy) DO UPDATE SET "
        "action=excluded.action, qty=excluded.qty, price=excluded.price, order_type=excluded.order_type, "
        "limit_price=excluded.limit_price, adaptive_priority=excluded.adaptive_priority, status=excluded.status"
    )
    con = _connect(SIGNALS_ORDERS_DB_PATH)
    try:
        cur = con.execute(
            sql,
            [
                symbol,
                ts_str,
                action,
                float(qty),
                float(price),
                strategy,
                order_type,
                None if limit_price is None else float(limit_price),
                adaptive_priority,
                status,
            ],
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


async def record_signal_async(
    symbol: str,
    ts_utc,
    action: str,
    qty: float,
    price: float,
    strategy: str = "sma",
    order_type: str = "MKT",
    limit_price: Optional[float] = None,
    adaptive_priority: Optional[str] = None,
    status: str = "NEW",
) -> int:
    return await asyncio.to_thread(
        record_signal,
        symbol,
        ts_utc,
        action,
        qty,
        price,
        strategy,
        order_type,
        limit_price,
        adaptive_priority,
        status,
    )


def fetch_signals(symbol: str) -> pd.DataFrame:
    """Return DataFrame [ts index] with columns: side, price.

    side is an alias of action to keep compatibility with charting code.
    """
    con = _connect(SIGNALS_ORDERS_DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT ts_utc, action AS side, price FROM signals WHERE symbol = ? ORDER BY ts_utc",
            con,
            params=[symbol],
        )
    finally:
        con.close()
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["ts_utc"], utc=True))
    return df[["side", "price"]]


async def fetch_signals_new(symbol: str) -> pd.DataFrame:
    """Fetch signals with status='NEW' including all relevant columns for order placement."""
    con = _connect(SIGNALS_ORDERS_DB_PATH)
    try:
        df = pd.read_sql_query(
            """
            SELECT symbol, ts_utc, action AS side, qty, price, strategy, order_type, limit_price, adaptive_priority, status
            FROM signals
            WHERE symbol = ? AND status = 'NEW'
            ORDER BY ts_utc
            """,
            con,
            params=[symbol],
        )
    finally:
        con.close()
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["ts_utc"], utc=True))
    return df


async def fetch_signals_new_all() -> pd.DataFrame:
    """Fetch all signals with status='NEW' across symbols.

    Returns a DataFrame with columns:
    [symbol, ts_utc, side, qty, price, strategy, order_type, limit_price, adaptive_priority, status]
    indexed by ts_utc as a UTC DatetimeIndex.
    """
    con = _connect(SIGNALS_ORDERS_DB_PATH)
    try:
        df = pd.read_sql_query(
            """
            SELECT symbol, ts_utc, action AS side, qty, price, strategy, order_type, limit_price, adaptive_priority, status
            FROM signals
            WHERE status = 'NEW'
            ORDER BY ts_utc
            """,
            con,
        )
    finally:
        con.close()
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["ts_utc"], utc=True))
    return df


async def mark_signal_status(symbol: str, ts_utc, strategy: str, status: str) -> None:
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    def _update():
        con = _connect(SIGNALS_ORDERS_DB_PATH)
        try:
            con.execute(
                "UPDATE signals SET status = ? WHERE symbol = ? AND ts_utc = ? AND strategy = ?",
                [status, symbol, ts_str, strategy],
            )
            con.commit()
        finally:
            con.close()
    await asyncio.to_thread(_update)


# ------------------------------ Fills (optional) ----------------------------
def _ensure_fills_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            fill_id    TEXT PRIMARY KEY,
            order_id   INTEGER,
            symbol     TEXT NOT NULL,
            ts_utc     TEXT NOT NULL,
            side       TEXT NOT NULL,
            qty        REAL NOT NULL,
            price      REAL NOT NULL
        );
        """
    )


def record_fill(
    fill_id: str,
    order_id: int,
    symbol: str,
    ts_utc,
    side: str,
    qty: float,
    price: float,
) -> int:
    ts = pd.Timestamp(ts_utc)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    side_u = side.upper()
    if side_u not in ("BUY", "SELL"):
        raise ValueError("side must be 'BUY' or 'SELL'")

    con = _connect(TRADES_DB_PATH)
    try:
        _ensure_fills_schema(con)
        cur = con.execute(
            """
            INSERT INTO fills(fill_id, order_id, symbol, ts_utc, side, qty, price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fill_id) DO UPDATE SET
                order_id=excluded.order_id,
                symbol=excluded.symbol,
                ts_utc=excluded.ts_utc,
                side=excluded.side,
                qty=excluded.qty,
                price=excluded.price
            """,
            [fill_id, int(order_id), symbol, ts_str, side_u, float(qty), float(price)],
        )
        con.commit()
        # also append to CSV for optional PnL-from-CSV path
        _append_fill_csv(fill_id, order_id, symbol, ts_str, side_u, qty, price)
        return cur.rowcount
    finally:
        con.close()


async def insert_fill_for_order(
    order_id: int,
    exec_id: str,
    ts_utc,
    side: str,
    qty: float,
    price: float,
    symbol: str,
) -> int:
    """Async wrapper to insert a fill; maps exec_id -> fill_id."""
    return await asyncio.to_thread(
        record_fill,
        exec_id,  # fill_id
        order_id,
        symbol,
        ts_utc,
        side,
        qty,
        price,
    )


def fetch_fills(symbol: str) -> pd.DataFrame:
    con = _connect(TRADES_DB_PATH)
    try:
        _ensure_fills_schema(con)
        df = pd.read_sql_query(
            "SELECT ts_utc, side, qty, price FROM fills WHERE symbol = ? ORDER BY ts_utc",
            con,
            params=[symbol],
        )
    finally:
        con.close()
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["ts_utc"], utc=True))
    return df[["side", "qty", "price"]]


# ------------------------------ PnL (optional) ------------------------------
def upsert_pnl(symbol: str, ts_utc, realized: float = 0.0, unrealized: float = 0.0) -> int:
    ts = pd.Timestamp(ts_utc)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    con = _connect(PNL_DB_PATH)
    try:
        cur = con.execute(
            """
            INSERT INTO pnl(symbol, ts_utc, realized, unrealized)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, ts_utc) DO UPDATE SET
                realized=excluded.realized,
                unrealized=excluded.unrealized
            """,
            [symbol, ts_str, float(realized), float(unrealized)],
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def fetch_pnl(symbol: str) -> pd.DataFrame:
    con = _connect(PNL_DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT ts_utc, realized, unrealized FROM pnl WHERE symbol = ? ORDER BY ts_utc",
            con,
            params=[symbol],
        )
    finally:
        con.close()
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["ts_utc"], utc=True))
    return df[["realized", "unrealized"]]
# ------------------------------ 5‑second bars --------------------------------
def record_5s_bar(symbol: str, ts_5s_utc, o, h, l, c, v, wap=0.0, count=0) -> None:
    """Append a raw 5‑second bar to ohlcv_5s and update the in‑memory 1‑minute snapshot.

    - Idempotent insert into ohlcv_5s (PRIMARY KEY on (symbol, ts_utc)).
    - Calls the same in‑memory minute updater used by the live streamer.
    - Also persists the current minute to ohlcv_1m so readers see progression.
    """
    ts5 = pd.Timestamp(ts_5s_utc)
    if getattr(ts5, 'tz', None) is None:
        ts5 = ts5.tz_localize("UTC")
    else:
        ts5 = ts5.tz_convert("UTC")
    ts5_str = ts5.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write 5s
    con = _connect(TRADES_DB_PATH)
    try:
        con.execute(
            """
            INSERT INTO ohlcv_5s(symbol, ts_utc, open, high, low, close, volume, wap, count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, ts_utc) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                wap=excluded.wap,
                count=excluded.count
            """,
            [symbol, ts5_str, float(o), float(h), float(l), float(c), float(v), float(wap), int(count)],
        )
        con.commit()
    finally:
        con.close()

    # Update the in‑memory minute and persist that minute state
    ts_min = ts5.floor("min")
    _upsert_minute_in_memory(symbol, ts_min, float(o), float(h), float(l), float(c), float(v))
    # Persist the minute snapshot so external readers can see ongoing progress
    # (This uses the single‑row flush path already present.)
    df = snapshot(symbol)
    if ts_min in df.index:
        row = df.loc[ts_min]
        persist = pd.DataFrame(
            {
                "open": [float(row["open"])],
                "high": [float(row["high"])],
                "low": [float(row["low"])],
                "close": [float(row["close"])],
                "volume": [float(row.get("volume", 0))],
                "vol_up": [float(row.get("vol_up", 0))],
                "vol_down": [float(row.get("vol_down", 0))],
            },
            index=pd.DatetimeIndex([ts_min]),
        )
        flush_minute_bars(symbol, persist)
