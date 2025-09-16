# --- indicators.py (tiny, event-friendly) ---
from collections import deque
from math import sqrt
from typing import Literal
import pandas as pd

class RollingSMA:
    def __init__(self, window: int):
        self.w = window
        self.buf = deque(maxlen=window)
        self.sum = 0.0

    def push(self, x: float) -> float | None:
        if len(self.buf) == self.w:
            self.sum -= self.buf[0]
        self.buf.append(x)
        self.sum += x
        return (self.sum / len(self.buf)) if self.buf else None

class RollingStd:
    # Welford is great; for simplicity use sum/sum2
    def __init__(self, window: int):
        self.w = window
        self.buf = deque(maxlen=window)
        self.sum = 0.0
        self.sum2 = 0.0

    def push(self, x: float) -> float | None:
        if len(self.buf) == self.w:
            old = self.buf[0]
            self.sum  -= old
            self.sum2 -= old*old
        self.buf.append(x)
        self.sum  += x
        self.sum2 += x*x
        n = len(self.buf)
        if n < 2: 
            return None
        mean = self.sum / n
        var  = self.sum2 / n - mean*mean
        return sqrt(var) if var > 0 else 0.0

class Bollinger:
    def __init__(self, window: int, k: float = 2.0):
        self.sma = RollingSMA(window)
        self.std = RollingStd(window)
        self.k = k

    def push(self, x: float):
        m  = self.sma.push(x)
        sd = self.std.push(x)
        if m is None or sd is None:
            return None
        return {
            "mid": m,
            "upper": m + self.k * sd,
            "lower": m - self.k * sd,
        }


# --- Correlation helpers ---
CorrMethod = Literal['pearson', 'spearman', 'kendall']

def corr_matrix(panel: pd.DataFrame, method: CorrMethod = 'pearson') -> pd.DataFrame:
    """Compute correlation matrix for columns of a DataFrame using the given method.

    - pearson: linear correlation
    - spearman: rank correlation
    - kendall: Kendall's tau
    """
    if panel is None or panel.empty:
        return pd.DataFrame()
    if method not in ('pearson', 'spearman', 'kendall'):
        method = 'pearson'
    try:
        return panel.corr(method=method)
    except Exception:
        # Fallback to pearson if method not supported by the dtype
        return panel.corr(method='pearson')
