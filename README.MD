# Python PAPER TRADING Candles + SMA Strategy & more (SQLite + Dash)

This repository houses an Interactive Brokers (IBKR) first stack: live futures data is streamed from TWS/Gateway, aggregated into 1‑minute candles, stored in SQLite (WAL mode), visualised in Dash, and traded via a simple SMA crossover strategy that can submit Adaptive market orders.

## Requirements

- Python 3.11+
- TWS or IB Gateway running with the API enabled

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (or export the variables) with at least:

```
HOST=127.0.0.1
PORT=7497         # 4002 for IB Gateway paper
CLIENT_ID=17
SYMBOLS=MES:CME,MNQ:CME,MCL:NYMEX
EXPIRY=202509    # lastTradeDateOrContractMonth
FAST_MA=5
SLOW_MA=12
```

## Run the live stack

```bash
python run.py
```

The script starts an IB streamer per symbol, keeps a rolling in-memory minute buffer, runs the SMA crossover strategy, and (when `START_ROUTER=true`) launches the IB order router that polls NEW signals and submits Adaptive market orders.

## Dash visualisation

To inspect the SQLite database or monitor the live stream:

```bash
READ_FROM_DB=true python dash_app.py
# open http://127.0.0.1:8050
```

The Dash app renders price/volume, SMA overlays, RSI, Aroon oscillator, and overlays signal markers while displaying the latest realised PnL from `pnl.db`.

## Database tooling

```bash
python sqlitedb_setup.py           # create tables (ohlcv_1m, ohlcv_5s, signals, pnl)
python testscripts/seed_db.py      # optional dummy minute bars & signals
sqlite3 data/trades.db "select count(*) from ohlcv_1m;"
sqlite3 data/signals_orders.db "select status, count(*) from signals group by status;"
```

## Key modules

- `streamer.py` / `brokers/ib/streamer_ib.py` – IBKR real-time bars with auto-reconnect and SQLite aggregation via `data/dbactivator.py`.
- `strategy.py` – SMA crossover strategy emitting NEW signals into `signals_orders.db`.
- `order_router.py` / `brokers/ib/order_router_ib.py` – polls NEW signals and places Adaptive orders through IB.
- `data/dbactivator.py` – minute-ring cache, SQLite helpers for OHLCV, signals, fills, and PnL.
- `dash_app.py` – read-only dashboard backed by SQLite.
- `testscripts/seed_db.py` – generates synthetic minute data and signals when markets are closed.

## Notes

- In-memory history is capped at 120 minutes (`MAX_MINUTES`).
- Orders default to quantity 1; adjust via `QTY`.
- Architecture diagram: update `mermaid_architecture.py` and run `python mermaid_architecture.py` to regenerate `architecture.svg`/`architecture.pdf`.

