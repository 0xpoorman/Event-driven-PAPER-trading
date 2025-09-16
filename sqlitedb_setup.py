import sqlite3
from pathlib import Path

TRADES_DB_PATH = Path("./data/trades.db")
SIGNALS_ORDERS_DB_PATH = Path("./data/signals_orders.db")
PNL_DB_PATH = Path("./data/pnl.db")


SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS ohlcv_1m (
    symbol   TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,            -- ISO8601 UTC timestamp
    open     REAL NOT NULL,
    high     REAL NOT NULL,
    low      REAL NOT NULL,
    close    REAL NOT NULL,
    volume   INTEGER NOT NULL,
    wap      REAL NOT NULL,
    count    INTEGER NOT NULL,
    vol_up   INTEGER NOT NULL,
    vol_down INTEGER NOT NULL,
    position REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_ts ON ohlcv_1m(symbol, ts_utc);

-- Optional raw 5‑second bars (for audit/backfill)
CREATE TABLE IF NOT EXISTS ohlcv_5s (
    symbol   TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,            -- ISO8601 UTC timestamp at 5s boundary
    open     REAL NOT NULL,
    high     REAL NOT NULL,
    low      REAL NOT NULL,
    close    REAL NOT NULL,
    volume   INTEGER NOT NULL,
    wap      REAL NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv5_symbol_ts ON ohlcv_5s(symbol, ts_utc);

"""

SCHEMA_SIGNALS_ORDERS = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    ts_utc    TEXT NOT NULL,
    action      TEXT NOT NULL,           -- 'BUY' | 'SELL'
    qty       REAL NOT NULL CHECK (qty > 0),
    price     REAL NOT NULL,
    strategy  TEXT NOT NULL DEFAULT 'sma',
    order_type        TEXT NOT NULL CHECK (order_type IN ('MKT','LMT','STP','STP_LMT')),
    limit_price       REAL,                  -- NULL for MKT
    adaptive_priority TEXT,                  -- e.g. 'Urgent'
    status            TEXT NOT NULL DEFAULT 'NEW',  -- NEW/SENT/ACK/FILLED/CANCELLED/REJECTED
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (symbol, ts_utc, strategy)
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts_utc);
"""


# Optional PnL DB (uncomment PNL_DB_PATH above if you want this now)
SCHEMA_PNL = """
CREATE TABLE IF NOT EXISTS pnl (
    symbol     TEXT NOT NULL,
    ts_utc     TEXT NOT NULL,          -- bar timestamp for attribution
    realized   REAL NOT NULL DEFAULT 0,
    unrealized REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_pnl_symbol_ts ON pnl(symbol, ts_utc);
"""

def init_db(db_path: Path, schema_sql: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Using `with` ensures the connection is closed automatically.
    with sqlite3.connect(db_path) as con:
        # One-time setup PRAGMAs for durability + concurrency
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute("PRAGMA journal_mode = WAL;")     # enables 1-writer / many-readers
        con.execute("PRAGMA synchronous = NORMAL;")   # good balance for WAL
        con.execute("PRAGMA busy_timeout = 3000;")    # ms to wait if DB is locked
        # Optional: enlarge page cache (negative = KB)
        con.execute("PRAGMA cache_size = -131072;")   # ~128MB cache

        con.executescript(schema_sql)

if __name__ == "__main__":
    init_db(TRADES_DB_PATH,  SCHEMA_TRADES)
    init_db(SIGNALS_ORDERS_DB_PATH, SCHEMA_SIGNALS_ORDERS)
    init_db(PNL_DB_PATH,  SCHEMA_PNL)
    # init_db(PNL_DB_PATH,     SCHEMA_PNL)  # enable when ready
    print("SQLite structures created.")

"""
-- Not executed here (structure-only). Example mapping:
-- Order(action='BUY', totalQuantity=100, orderType='MKT', adaptivePriority='Urgent')
-- becomes a row in orders with:
-- action='BUY', qty=100, order_type='MKT', adaptive_priority='Urgent', limit_price=NULL
-- and (symbol, ts_utc, strategy) matching the parent signal.
Hierarchy: signals → orders → order_events (fills still belong in trades.db as you set up).

Simple & future-proof: if you later need broker-specific fields, add a column or stuff JSON in details without schema churn.

Async note: schema is unchanged; WAL already supports one writer + many readers.
"""
