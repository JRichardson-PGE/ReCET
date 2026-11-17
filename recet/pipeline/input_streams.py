"""Input streams (COST_TESTS.md section 3 and step 4): the value streams a cost-test set reads straight from Measure.csv columns, priced from the set.

A stream with `source: input` names the per-unit columns it adds up (`columns`, and `columns_2nd` for the 2nd-baseline years), a `basis`, the NTG it is
scaled by (`ntg`: kwh = NTGRkWh, therm = NTGRTherm, none = 1) and the market effects added to that NTG (`market_effects`: benefits = the run's MEBens,
costs = its MECost, none). Per measure, with Q = NumUnits, F = NTG + ME (1 for ntg none) and a1 / a2 the sums of the columns / columns_2nd per unit:

    one_time            value = Q x F x a1                                                     undiscounted, as CET's OtherBen / OtherCost / TaxCredits (C7)
    annual_over_life    value = Q x F x L,   L = a1 x A(EUL) / Rqf^Qm                          RUL = 0 (or RUL >= EUL: rul_in_use)
                                             L = ( a1 x A(RUL) + a2 x A(EUL - RUL) / Raf^RUL ) / Rqf^Qm     RUL > 0
                        A(n) = 1 + (1 - Raf^-(n - 1)) / Ra    (n years at a1 a year, the first undiscounted; A(0) = 0; n when Ra = 0)

a2 = a1 when the stream has no columns_2nd. Ra / Raf = the pricing's annual rate and 1 + it, Rqf = 1 + its quarterly rate (the run-parameter columns the
pricing names, per PA and ACC version), Qm = the claim quarter's offset from the run's First Year: the per-year amount discounted as CalcCost discounts the
savings (run_poc.discounted_savings, without CET's "0 when a1 is 0" rule), so year one is valued at the claim quarter and the whole brought back to Q1 of
the First Year, like every avoided-cost benefit. The gross form (a stream with `gross`) is the same with F = 1.

`price(measures, cset, ...)` writes every input stream of the set in each of its pricings (cset.column names). EE-2026's three input streams (OtherBen,
OtherCost, TaxCredits) give exactly engine.price_other_ben_cost's values (same arithmetic in the same order; tests/test_input_streams.py checks them bit
for bit), so a run on EE-2026 is unchanged; a set that adds a stream (a non-energy benefit, say) gets its columns in the measure-level table and in the
tests that name it. A column the set names that Measure.csv lacks is read as 0 (jobs.check_input_columns notes it; a value that is not a number stops the
run before pricing).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import discount_settings
from .engine import _column_or_default, rul_in_use

NTG_COLUMN = {"kwh": "NTGRkWh", "therm": "NTGRTherm"}


def input_stream_names(cset) -> list[str]:
    return [s.name for s in cset.streams.values() if s.source == "input"]


def _unit_sum(m: pd.DataFrame, columns) -> np.ndarray:
    """The per-unit columns added left to right (a + b + c, as engine.price_other_ben_cost adds them)."""
    total = _column_or_default(m, columns[0])
    for c in columns[1:]:
        total = total + _column_or_default(m, c)
    return total


def _annual_sum(n, rate_annual):
    """A(n): n years at one a year, the first undiscounted (sum of Raf^-t for t = 0 .. n-1, continued to fractional n); 0 for n <= 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        a = np.where(rate_annual == 0, n, 1.0 + (1.0 - (1.0 + rate_annual) ** -(n - 1.0)) / np.where(rate_annual == 0, 1.0, rate_annual))
    return np.where(n > 0, a, 0.0)


def _rates(m: pd.DataFrame, pricing, acc_version: int) -> tuple[np.ndarray, np.ndarray]:
    pas = m["IOU_AC_Territory"].astype(str)
    settings = {pa: discount_settings.get_settings(pa, acc_version) for pa in pas.unique()}
    annual = pas.map({pa: s[pricing.discount_rate_annual] for pa, s in settings.items()}).astype(float).to_numpy()
    quarterly = pas.map({pa: s[pricing.discount_rate_quarterly] for pa, s in settings.items()}).astype(float).to_numpy()
    return annual, quarterly


def life_factor(m: pd.DataFrame, a1: np.ndarray, a2: np.ndarray, rate_annual: np.ndarray, rate_qtr: np.ndarray, base_year: int) -> np.ndarray:
    """L per unit: a1 a year (a2 after the RUL) over the measure's life, discounted to Q1 of the First Year (the module docstring)."""
    eul = _column_or_default(m, "EUL_Yrs")
    rul = rul_in_use(_column_or_default(m, "RUL_Yrs"), eul)
    raf = 1.0 + rate_annual
    dual = a1 * _annual_sum(rul, rate_annual) + a2 * _annual_sum(eul - rul, rate_annual) / raf ** rul
    single = a1 * _annual_sum(eul, rate_annual)
    quarters_from_first_year = (_column_or_default(m, "Used_Year") - base_year) * 4 + (_column_or_default(m, "Used_Quarter") - 1)
    return np.where(rul > 0, dual, single) / (1.0 + rate_qtr) ** quarters_from_first_year


def _factor(m: pd.DataFrame, stream, market_effects_bens: float, market_effects_cost: float):
    me = {"benefits": market_effects_bens, "costs": market_effects_cost}.get(stream.market_effects, 0.0)
    if stream.ntg == "none":
        return None if not me else 1.0 + me
    return _column_or_default(m, NTG_COLUMN[stream.ntg]) + me


def price(measures: pd.DataFrame, cset, acc_version: int, base_year: int, market_effects_bens: float = 0.0,
          market_effects_cost: float = 0.0) -> pd.DataFrame:
    """CEInputID and every input stream of `cset` in each of its pricings, with its gross form (the module docstring)."""
    m = measures
    out = {"CEInputID": m["CEInputID"].to_numpy()}
    rates: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    NumUnits = _column_or_default(m, "NumUnits")
    for s in cset.streams.values():
        if s.source != "input":
            continue
        a1 = _unit_sum(m, s.columns)
        a2 = _unit_sum(m, s.columns_2nd) if s.columns_2nd else a1
        f = _factor(m, s, float(market_effects_bens or 0.0), float(market_effects_cost or 0.0))
        for p in s.pricings:
            col = cset.column(s.name, p)
            if col in out:                                   # naming none: one column for every pricing (validate: same rates)
                continue
            if s.basis == "one_time":
                per_unit = a1
            else:
                if p not in rates:
                    rates[p] = _rates(m, cset.pricings[p], acc_version) if len(m) else (np.zeros(0), np.zeros(0))
                per_unit = life_factor(m, a1, a2, *rates[p], base_year)
            out[col] = NumUnits * per_unit if f is None else NumUnits * f * per_unit
            if s.gross is not None and s.gross != "same":
                out[cset.column(s.name, p, gross=True)] = NumUnits * per_unit
    return pd.DataFrame(out)


def overlay(frame: pd.DataFrame, streams: pd.DataFrame, only_existing: bool = False) -> pd.DataFrame:
    """`frame` with the stream columns of `streams` (price) put in: an existing column is replaced where it stands, a new one appended (unless
    `only_existing`). Rows are matched on CEInputID."""
    cols = [c for c in streams.columns if c != "CEInputID" and (c in frame.columns or not only_existing)]
    if not cols:
        return frame
    if len(frame) == len(streams) and (frame["CEInputID"].to_numpy() == streams["CEInputID"].to_numpy()).all():
        aligned = streams
    else:
        aligned = frame[["CEInputID"]].merge(streams, on="CEInputID", how="left")
    return frame.assign(**{c: aligned[c].to_numpy() for c in cols})


def extra_columns(cset) -> list[str]:
    """The Measure.csv columns the set's input streams read that are not CET's (or ReCET's own optional) Measure fields: the loader keeps them,
    blank = 0, and the template adds them."""
    from ..templates import MEASURE_COLUMNS
    known = set(MEASURE_COLUMNS)
    return [c for c in cset.input_columns() if c not in known]


def check_columns(cset, files) -> tuple[list[str], dict[str, tuple[int, list[str]]]]:
    """(the set's extra input columns that no Measure file has, {column: (values that are not numbers, up to 3 examples)}) over `files` (the job's
    Measure.csv, or its parts). Blank is 0, not an error."""
    from .delimited import header_columns, read_cet_csv
    cols = extra_columns(cset)
    if not cols:
        return [], {}
    seen, bad = set(), {}
    for f in files:
        present = [c for c in cols if c in header_columns(f)]
        seen.update(present)
        if not present:
            continue
        raw = read_cet_csv(f, usecols=present, dtype=str, keep_default_na=False)
        for c in present:
            v = raw[c].str.strip()
            v = v[v != ""]
            wrong = v[pd.to_numeric(v, errors="coerce").isna()]
            if len(wrong):
                n, ex = bad.get(c, (0, []))
                bad[c] = (n + len(wrong), (ex + list(wrong.unique()[:3]))[:3])
    return [c for c in cols if c not in seen], bad
