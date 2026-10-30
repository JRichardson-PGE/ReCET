"""The regression gate: real jobs on slices of the real reference data (tests/data/regression, built by `recet regress build` on a machine with the full
data; README there) give their expected outputs, within regress.TOLERANCE. A deliberate change of results rewrites them with `recet regress check --update`
and says why in the PR."""
from __future__ import annotations

from pathlib import Path

import pytest

from recet import regress

FIXTURES = Path(__file__).parent / "data" / "regression"


@pytest.mark.skipif(not (FIXTURES / "manifest.json").exists(), reason="no regression fixtures yet (recet regress build)")
def test_real_jobs_give_their_expected_outputs(tmp_path):
    lines = []
    result = regress.check(FIXTURES, tmp_path, log=lines.append)
    assert result and not any(result.values()), "\n".join(lines)
