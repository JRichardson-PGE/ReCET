"""Applying a run's configuration to the pipeline, and resetting it between runs.

The pipeline modules cache heavily (the ACC parquet, profile shapes ...) and read their file locations through `recet.paths`. To run more than one
ACC version -- or the same one with different supplied profiles -- in a single process, `apply()` points the paths and user overrides at this run's inputs and
`clear_caches()` drops everything derived from the previous run. (The GUI and `run_both` avoid the question by running each job in its own process.)
"""
from __future__ import annotations

import importlib

from . import paths

_PIPELINE_MODULES = ("real_avoided_cost_v2", "gas_avoided_cost", "real_impact_profiles", "gas_savings_profiles", "emissions",
                     "discount_settings", "rate_schedules", "statewide_allocation", "cost_tests", "system_zones")


def clear_caches() -> int:
    """Call cache_clear() on every lru_cached function in the pipeline modules; returns how many were cleared."""
    n = 0
    for name in _PIPELINE_MODULES:
        mod = importlib.import_module(f"{__package__}.pipeline.{name}")
        for attr in vars(mod).values():
            if callable(attr) and hasattr(attr, "cache_clear"):
                attr.cache_clear()
                n += 1
    return n


_LAST: dict = {}


def switch_acc(acc) -> None:
    """Point the pipeline at another ACC version, keeping the overrides of the last `apply` (used to price a job's measures on their own ACC versions)."""
    apply(acc, **_LAST)


def apply(acc, *, user_profiles=None, user_gas_profiles=None, run_parameters=None, sw_factors=None, programs=None, reference_dir=None,
          sw_factors_replace: bool = False, programs_replace: bool = False) -> None:
    """Point the pipeline at this run's ACC files and overrides.

    acc: versions.ResolvedAcc. Every electric measure, SCG included, is priced on the 8,760 path. user_profiles: overrides.UserProfiles or None. user_gas_profiles: {name: array(12)} or None.
    run_parameters: overrides.load_run_parameters' table or None. sw_factors: overrides.load_sw_factors' table or None; sw_factors_replace: that table is the
    whole factor table (else its rows replace / extend the built-in one).
    programs: programs.load_programs' table or None; programs_replace: that table is the whole program table."""
    global _LAST
    _LAST = dict(user_profiles=user_profiles, user_gas_profiles=user_gas_profiles, run_parameters=run_parameters, sw_factors=sw_factors, programs=programs,
                 sw_factors_replace=sw_factors_replace, programs_replace=programs_replace)
    if reference_dir is not None:
        paths.set_reference_dir(reference_dir)
    paths.configure_runtime(acc_electric=acc.electric, acc_gas=acc.gas)
    from .pipeline import discount_settings, gas_savings_profiles, real_impact_profiles, statewide_allocation
    real_impact_profiles.set_user_profiles(user_profiles)
    gas_savings_profiles.set_user_profiles(user_gas_profiles)
    discount_settings.set_user_parameters(run_parameters)
    statewide_allocation.set_user_factors(sw_factors, replace=sw_factors_replace)
    from . import programs as programs_mod
    programs_mod.set_user_programs(programs, replace=programs_replace and programs is not None)
    clear_caches()
