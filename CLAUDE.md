# ReCET

ReCET ("reset") is the rebuilt CEDARS Cost Effectiveness Tool: it prices CET-format inputs (Measure / ProgramCost, or one Excel job workbook)
hour by hour on the ACC and writes CET's outputs per measure, with a tkinter GUI (`python -m recet`) and a CLI (`recet run|preflight|template|check|data`).
It was moved out of the CEDARS review research repository (`JRichardson-PGE/CEDARS-Research`, folder `poc/`, package then called `cedars_poc`) on
2026-10-09 with a fresh history. The research behind it (ACC extraction and validation, CEDARS quarterly-avoided-cost reproduction, CET error findings,
TOU work) stays in that repo; its CLAUDE.md and `poc/README.md` hold the evidence for the methodology.

## Rules that keep the tool honest

- The docs in `recet/docs/` are checked by tests. Change a formula and you change `CALCULATIONS.md`, the field's entry in `OUTPUT_FIELDS.md`, and
  `tests/reference_calc.py` (an independent loop implementation) with it. `tests/test_output_docs.py` re-runs the worked examples in `tests/doc_examples.py`.
- Pricing arithmetic exists twice: the grouped versions (`engine.price_real_hourly`, `price_gas_benefit`) and the per-measure references
  (`..._per_measure`). Change both; the tests compare them.
- Reproduce CET's real behaviour unless a deliberate, documented exception says otherwise (e.g. SCB/SCH priced on the real SCT rows, not CET's TRC copy).
- Large files are never in git: the two electric ACC parquets and the ~9 GB impact-profile library live in the *reference data folder*
  (`recet data status`, `RECET_DATA`, `~/.recet/config.json`). Small tables ship in `recet/data/`.
- Research-only code does not belong here. On 2026-10-09 the first proof of concept's pieces (synthetic shapes and pricers, the first-generation ACC
  loaders, the TOU quarterly profiles, the research datasets, the old `run_poc` command line) moved to the research repo's `poc/research_lib`, and
  the retired TOU/SCG quarterly routes, the CET-CO2 comparison option and the research-checkout file fallback were removed. Research scripts in
  CEDARS-Research import `recet` for the tool's functions and `research_lib` for the rest.
- The Edit inputs tab is for small jobs (up to 500 measures); larger files are edited offline.
- Tests: `pip install -e ".[test]"` then `pytest` from the repo root; they need no reference data.
