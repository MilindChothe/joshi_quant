"""20 SMA signal logic with 0.1% threshold band."""
import pandas as pd


def sma_signal(df: pd.DataFrame, period: int = 20, threshold_pct: float = 0.1) -> str:
    """
    df must have 'close' column, 5-min candles, oldest->newest.
    Returns 'LONG' if last close > SMA*(1+thr), 'SHORT' if < SMA*(1-thr), else 'HOLD'.
    HOLD means keep previous position (inside band = no new signal).
    """
    if df is None or len(df) < period:
        return "HOLD"
    sma = df["close"].rolling(period).mean().iloc[-1]
    last = float(df["close"].iloc[-1])
    band = float(sma) * float(threshold_pct) / 100.0
    if last > sma + band:
        return "LONG"
    if last < sma - band:
        return "SHORT"
    return "HOLD"


def add_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    out = df.copy()
    out["sma"] = out["close"].rolling(period).mean()
    return out
