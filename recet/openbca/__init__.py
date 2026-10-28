"""OpenBCA hosted by ReCET (CEDARS-Research scope "OpenBCA module for ReCET", Jake 2026-10-28).

OpenBCA (github.com/openbca/openbca, NASEO / LBNL, BSD licence) runs unchanged through its Integrated pathway: its SQLmesh projects (`excel_input_parsing/`
and `core/`) on DuckDB. ReCET never imports SQLmesh or DuckDB: `run.py` stages a copy of a pinned OpenBCA checkout in the run folder and runs `engine.py`
with that checkout's own interpreter, in its own process, then writes OpenBCA's own output tables in ReCET's file formats.
"""
