import io
import os
import traceback
import sqlite3
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, State

# ---- Environment / paths ----
TRADES_DB_PATH = Path(os.getenv("TRADES_DB_PATH", "./data/trades.db")).resolve()
SIGNALS_DB_PATH = Path(os.getenv("SIGNALS_DB_PATH", "./data/signals_orders.db")).resolve()
PNL_DB_PATH = Path(os.getenv("PNL_DB_PATH", "./data/pnl.db")).resolve()

READ_FROM_DB = os.getenv("READ_FROM_DB", "true").lower() == "true"
DEFAULT_TF = os.getenv("DEFAULT_TF", "1m")
SYMBOLS_ENV = os.getenv("SYMBOLS", "MES:CME,MNQ:CME,MCL:NYMEX")
# keep just the product code before ':'
ALL_SYMBOLS = [s.split(":")[0].strip() for s in SYMBOLS_ENV.split(",") if s.strip()] or ["MES"]

# ---- Dash app ----
app = dash.Dash(__name__, suppress_callback_exceptions=True)
app.title = "IBK Live — Dashboard"

# ---- Basic DB helpers ----
def _connect(path: Path):
    return sqlite3.connect(path.as_posix())

def load_bars(symbol: str, limit: int = 3000) -> pd.DataFrame:
    """
    Load latest 1-minute bars for a symbol.
    Expects columns: ts_utc, open, high, low, close, volume, (optional) vol_up, vol_down
    """
    with _connect(TRADES_DB_PATH) as con:
        df = pd.read_sql_query(
            """
            SELECT ts_utc, open, high, low, close, volume, 
                   COALESCE(vol_up, NULL)  AS vol_up,
                   COALESCE(vol_down, NULL) AS vol_down
            FROM ohlcv_1m
            WHERE symbol = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            con, params=(symbol, limit),
        )
    if df.empty:
        return df
    df = df.sort_values("ts_utc")
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts_utc")
    # build vol_up/vol_down if missing or broken
    if ("vol_up" not in df.columns) or ("vol_down" not in df.columns) or df["vol_up"].isna().all():
        up_mask = (df["close"] >= df["open"])
        df["vol_up"] = df["volume"].where(up_mask, 0.0)
        df["vol_down"] = df["volume"].where(~up_mask, 0.0)
    # guard against negatives / NaNs
    df["vol_up"] = df["vol_up"].clip(lower=0).fillna(0.0)
    df["vol_down"] = df["vol_down"].clip(lower=0).fillna(0.0)
    return df

def load_many_bars(symbols: List[str], limit: int = 3000) -> dict:
    return {sym: load_bars(sym, limit=limit) for sym in symbols}

def load_pnl() -> pd.DataFrame:
    """
    Expecting a table 'pnl' with columns:
      ts_utc TEXT, symbol TEXT, strategy TEXT, realized REAL
    """
    if not PNL_DB_PATH.exists():
        return pd.DataFrame(columns=["ts_utc","symbol","strategy","realized"])
    with _connect(PNL_DB_PATH) as con:
        try:
            df = pd.read_sql_query(
                "SELECT ts_utc, symbol, strategy, realized FROM pnl ORDER BY ts_utc",
                con,
            )
        except Exception:
            return pd.DataFrame(columns=["ts_utc","symbol","strategy","realized"])
    if df.empty:
        return df
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df

def load_signals() -> pd.DataFrame:
    """
    Optional: read signals if you want win ratio by signals table.
    Assumed minimal schema: signals(symbol, strategy, side, status, ts_utc, pnl_realized)
    We'll keep this optional to stay simple.
    """
    if not Path(SIGNALS_DB_PATH).exists():
        return pd.DataFrame()
    try:
        with _connect(SIGNALS_DB_PATH) as con:
            df = pd.read_sql_query(
                "SELECT * FROM signals ORDER BY ts_utc",
                con,
            )
        if "ts_utc" in df.columns:
            df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()

# ---- Figures ----
def make_market_figure(df: pd.DataFrame, title: str, ypad: float = 0.02) -> go.Figure:
    if df is None or df.empty:
        return go.Figure(layout=dict(title="No data"))
    # Build figure with candlesticks + stacked vol + RSI
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.68, 0.22, 0.10],
        vertical_spacing=0.03,
        specs=[[{"type": "xy"}],[{"type": "bar"}],[{"type": "xy"}]],
        subplot_titles=[title, "Volume (stacked)", "RSI(14)"]
    )
    # Price
    fig.add_trace(
        go.Candlestick(
            x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="Price"
        ), row=1, col=1
    )
    # Simple MAs
    closes = df["close"].astype(float)
    for win, color in [(5, None), (12, None)]:
        ma = closes.rolling(win).mean()
        fig.add_trace(go.Scatter(x=df.index, y=ma, mode="lines", name=f"MA{win}", line=dict(width=1.2)), row=1, col=1)

    # RSI
    delta = closes.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    n = 14
    roll_up = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = (roll_up / roll_down).replace([np.inf, -np.inf], np.nan)
    rsi = 100 - (100 / (1 + rs))
    fig.add_trace(go.Scatter(x=df.index, y=rsi, mode="lines", name="RSI(14)", line=dict(width=1.0)), row=3, col=1)
    fig.add_hrect(y0=30, y1=70, line_width=0, fillcolor="LightGray", opacity=0.15, row=3, col=1)

    # Volume stacked (fixing breaks) + set global barmode
    fig.add_trace(go.Bar(x=df.index, y=df["vol_up"], name="Vol+", opacity=0.9), row=2, col=1)
    fig.add_trace(go.Bar(x=df.index, y=df["vol_down"], name="Vol-", opacity=0.9), row=2, col=1)
    fig.update_layout(barmode="relative")

    # Flexible y-axis padding for price & volume so bars don't look tiny
    pmin, pmax = float(np.nanmin(df["low"])), float(np.nanmax(df["high"]))
    pad = (pmax - pmin) * (ypad if np.isfinite(pmax - pmin) else 0.02)
    if np.isfinite(pad) and pad > 0:
        fig.update_yaxes(range=[pmin - pad, pmax + pad], row=1, col=1)
    # volume dynamic range
    vmax = float(np.nanmax(df[["vol_up","vol_down"]].sum(axis=1)))
    vpad = vmax * 0.10 if np.isfinite(vmax) else None
    if vpad:
        fig.update_yaxes(range=[0, vmax + vpad], row=2, col=1)

    fig.update_layout(
        margin=dict(l=40, r=10, t=40, b=30),
        xaxis_rangeslider_visible=False,
        uirevision="market",  # preserve zoom on updates
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        template="plotly_white",
    )
    return fig

def make_corr_heatmap(dfs: dict, method: str = "pearson") -> go.Figure:
    # Align on time and compute returns
    closes = []
    labels = []
    for sym, df in dfs.items():
        if df is not None and not df.empty:
            labels.append(sym)
            closes.append(df["close"].astype(float).rename(sym))
    if not closes:
        return go.Figure(layout=dict(title="No data"))
    mat = pd.concat(closes, axis=1).dropna()
    rets = mat.pct_change().dropna()
    method = (method or "pearson").lower()
    if method == "pearson":
        corr = rets.corr(method="pearson")
    elif method == "spearman":
        corr = rets.corr(method="spearman")
    else:
        corr = rets.corr(method="kendall")
    fig = go.Figure(data=go.Heatmap(z=corr.values, x=corr.columns, y=corr.index, zmin=-1, zmax=1, colorbar=dict(title="ρ")))
    fig.update_layout(title=f"Correlation heatmap ({method.title()})", template="plotly_white", margin=dict(l=60,r=20,t=50,b=40))
    return fig

def sharpe_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252*24*60) -> float:
    # assumes returns are per bar (1m). Keep simple: excess mean / std * sqrt(annualization)
    if returns.empty or returns.std(ddof=0) == 0:
        return float("nan")
    ex = returns - rf/periods_per_year
    return np.sqrt(periods_per_year) * ex.mean() / ex.std(ddof=0)

def sortino_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252*24*60) -> float:
    if returns.empty:
        return float("nan")
    ex = returns - rf/periods_per_year
    downside = ex[ex < 0]
    denom = downside.std(ddof=0)
    if denom == 0 or np.isnan(denom):
        return float("nan")
    return np.sqrt(periods_per_year) * ex.mean() / denom

def make_performance_tab(pnl: pd.DataFrame) -> Tuple[go.Figure, pd.DataFrame]:
    if pnl is None or pnl.empty:
        return go.Figure(layout=dict(title="No PnL yet")), pd.DataFrame(columns=["strategy","symbol","cum_pnl","sharpe","sortino","win_ratio"])
    pnl = pnl.copy()
    pnl["realized"] = pd.to_numeric(pnl["realized"], errors="coerce").fillna(0.0)
    pnl["cum_pnl"] = pnl.groupby(["strategy","symbol"])["realized"].cumsum()
    # proxy returns as pnl normalized by rolling abs pnl to stay simple
    pnl["ret"] = pnl.groupby(["strategy","symbol"])["realized"].apply(lambda s: s / (s.abs().rolling(100, min_periods=5).mean().replace(0, np.nan)))
    agg = []
    for (st, sym), g in pnl.groupby(["strategy","symbol"]):
        r = g["ret"].dropna()
        wins = (g["realized"] > 0).sum()
        total = (g["realized"] != 0).sum()
        win_ratio = (wins / total) if total > 0 else np.nan
        agg.append({
            "strategy": st,
            "symbol": sym,
            "cum_pnl": g["cum_pnl"].iloc[-1],
            "sharpe": sharpe_ratio(r) if not r.empty else np.nan,
            "sortino": sortino_ratio(r) if not r.empty else np.nan,
            "win_ratio": win_ratio,
        })
    perf = pd.DataFrame(agg).sort_values(["strategy","symbol"]).reset_index(drop=True)

    # Running realized PnL chart (by strategy & product)
    last_curves = []
    for (st, sym), g in pnl.groupby(["strategy","symbol"]):
        last_curves.append(go.Scatter(x=g["ts_utc"], y=g["cum_pnl"], mode="lines", name=f"{st}:{sym}"))
    fig = go.Figure(last_curves)
    fig.update_layout(title="Running Realized PnL (by Strategy:Product)", template="plotly_white", margin=dict(l=50,r=20,t=50,b=40), legend=dict(orientation="h"))
    return fig, perf

# ---- Layout ----
app.layout = html.Div([
    html.Div([
        html.Div([
            html.Label("Symbol"),
            dcc.Dropdown(
                id="symbol",
                options=[{"label": s, "value": s} for s in ALL_SYMBOLS],
                value=ALL_SYMBOLS[0] if ALL_SYMBOLS else None,
                multi=False, clearable=False,
            ),
        ], style={"minWidth":"180px","marginRight":"10px"}),
        html.Div([
            html.Label("Compare (for correlations)"),
            dcc.Dropdown(
                id="compare_symbols",
                options=[{"label": s, "value": s} for s in ALL_SYMBOLS],
                value=ALL_SYMBOLS[:min(4, len(ALL_SYMBOLS))],
                multi=True,
            ),
        ], style={"minWidth":"260px","flex":"1"}),
        html.Div([
            html.Label("Timeframe"),
            dcc.Dropdown(id="tf", options=[{"label": x, "value": x} for x in ["1m","5m","15m","30m","1H","4H","1D"]], value=DEFAULT_TF, clearable=False),
        ], style={"minWidth":"140px","marginLeft":"10px"}),
    ], style={"display":"flex","alignItems":"end","gap":"12px","padding":"10px 10px 0"}),

    dcc.Tabs(id="tabs", value="market", children=[
        dcc.Tab(label="Market", value="market"),
        dcc.Tab(label="Correlations", value="corr"),
        dcc.Tab(label="Performance", value="perf"),
    ]),

    html.Div(id="tab-content", style={"padding":"8px 10px"}),
])

# ---- Callbacks ----
@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value"),
    State("symbol", "value"),
    State("compare_symbols", "value"),
    prevent_initial_call=False,
)
def render_tab(tab, symbol, compare_symbols):
    try:
        if tab == "market":
            df = load_bars(symbol) if symbol else pd.DataFrame()
            fig = make_market_figure(df, f"{symbol} — {DEFAULT_TF}" if symbol else "Market")
            return dcc.Graph(figure=fig, config={"displaylogo": False})
        elif tab == "corr":
            syms = list(dict.fromkeys((compare_symbols or []) + ([symbol] if symbol else [])))
            dfs = load_many_bars(syms)
            corr_dropdown = dcc.Dropdown(
                id="corr_method", options=[{"label": m, "value": m} for m in ["pearson","spearman","kendall"]],
                value="pearson", clearable=False, style={"width":"220px"}
            )
            # nested graph will update via a small sub-callback
            return html.Div([
                html.Div([html.Label("Correlation method"), corr_dropdown], style={"marginBottom":"10px"}),
                dcc.Graph(id="corr_graph", config={"displaylogo": False}),
                dcc.Store(id="corr_cache", data={k: v.to_csv() if isinstance(v, pd.DataFrame) else "" for k,v in dfs.items()})
            ])
        else:  # perf
            pnl = load_pnl()
            fig, table = make_performance_tab(pnl)
            # simple HTML table
            header = [html.Th(col) for col in table.columns]
            rows = [html.Tr([html.Td(table.iloc[i][col]) for col in table.columns]) for i in range(len(table))]
            return html.Div([
                dcc.Graph(figure=fig, config={"displaylogo": False}),
                html.Hr(),
                html.H4("By Strategy and Product"),
                html.Table([html.Thead(html.Tr(header)), html.Tbody(rows)], style={"width":"100%","borderCollapse":"collapse"}),
            ])
    except Exception as e:
        traceback.print_exc()
        return html.Pre(str(e))

# sub-callback for correlation method
@app.callback(
    Output("corr_graph", "figure"),
    Input("corr_method", "value"),
    State("corr_cache", "data"),
    prevent_initial_call=True,
)
def update_corr_graph(method, cache):
    try:
        # Rehydrate cached CSVs
        dfs = {}
        for sym, csv in (cache or {}).items():
            if csv:
                df = pd.read_csv(io.StringIO(csv), index_col=0, parse_dates=True)
                dfs[sym] = df
        fig = make_corr_heatmap(dfs, method=method)
        return fig
    except Exception:
        traceback.print_exc()
        return go.Figure(layout=dict(title="Error building correlation"))

if __name__ == "__main__":
    debug_flag = os.getenv("DASH_DEBUG", "false").lower() == "true"
    app.run(debug=debug_flag)
