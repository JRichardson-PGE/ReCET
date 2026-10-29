"""SQL Server behaviour the CET replicas need to reproduce RunCET to the last digit.

- NULL is NaN. A CASE WHEN takes its branch only when the test is TRUE, so a comparison with NULL falls to ELSE; numpy's
  NaN comparisons are False, which gives the same branch as long as no NOT is applied to a NULL-able comparison.
- SUM skips NULLs and is NULL when every value is NULL (pandas ``sum(min_count=1)``).
- UNION removes duplicate rows (``drop_duplicates`` over the selected columns).
- CET's databases use SQL_Latin1_General_CP1_CI_AS: text comparisons ignore case and trailing spaces (`key`).
- POWER returns the type of its first argument. CET's TRC discount rates are decimal(18,6), so POWER(Rqf, n) is a
  decimal(38,6): the power rounded to six places (`dec_pow`). The societal rates are float and are not rounded.
- 1.000 / COUNT(...) is a decimal division with 14 places (`one_over`).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

NaN = np.nan
_SIX = Decimal("0.000001")
_POW: dict[tuple[float, float], float] = {}


def key(s) -> pd.Series:
    """A text column as CET's collation compares it: case-insensitive, trailing spaces ignored (spaces only: a trailing
    line break counts, so 24.3's 'Commercial\\n' gas sectors match no measure). NULL stays NULL."""
    s = pd.Series(s)
    return s.astype("string").str.rstrip(" ").str.casefold()


def dec_pow(base, n) -> np.ndarray:
    """POWER(decimal(.,6) base, n): the power rounded half up to six decimal places (NULL in, NULL out)."""
    b = np.broadcast_to(np.asarray(base, dtype=float), np.shape(n)).ravel()
    k = np.asarray(n, dtype=float).ravel()
    out = np.empty(len(k))
    for i, (bb, kk) in enumerate(zip(b, k)):
        if np.isnan(bb) or np.isnan(kk):
            out[i] = NaN
            continue
        hit = _POW.get((bb, kk))
        if hit is None:
            p = bb ** (int(kk) if kk == int(kk) else kk)
            hit = _POW[(bb, kk)] = float(Decimal(p).quantize(_SIX, rounding=ROUND_HALF_UP))
        out[i] = hit
    return out.reshape(np.shape(n))


def pos(x) -> np.ndarray:
    """CASE WHEN ISNULL(x, 0) > 0 THEN x ELSE 0 END"""
    x = np.asarray(x, dtype=float)
    return np.where(np.nan_to_num(x) > 0, x, 0.0)


def z(x):
    """ISNULL(x, 0)"""
    return x.fillna(0) if isinstance(x, (pd.Series, pd.DataFrame)) else np.nan_to_num(np.asarray(x, dtype=float))


def trunc(x) -> np.ndarray:
    """CONVERT(INT, x): truncation toward zero."""
    return np.trunc(np.asarray(x, dtype=float))


def one_over(n) -> np.ndarray:
    """1.000 / n with n an integer count (a decimal with 14 places)."""
    return np.round(1.0 / np.asarray(n, dtype=float), 14)


def dec10_mod1(x) -> np.ndarray:
    """CONVERT(DECIMAL(13,10), x) % 1"""
    return np.round(np.asarray(x, dtype=float), 10) % 1


def divide_or_zero(num, den) -> np.ndarray:
    """CASE WHEN den <> 0 THEN num / den ELSE 0 END (a NULL den gives 0)."""
    den = np.asarray(den, dtype=float)
    ok = np.nan_to_num(den) != 0
    return np.where(ok, np.asarray(num, dtype=float) / np.where(ok, den, 1.0), 0.0)


def group_sum(df: pd.DataFrame, by, cols) -> pd.DataFrame:
    """SELECT by, SUM(col) ... GROUP BY by"""
    return df.groupby(by, sort=False, dropna=False)[cols].sum(min_count=1).reset_index()


def number(s, default=None) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce")
    return v if default is None else v.fillna(default)
