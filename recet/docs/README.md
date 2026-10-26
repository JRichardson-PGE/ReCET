# recet documentation

| Document | Read it to learn |
|---|---|
| [Getting started on the web](WEB_GUIDE.md) | the web site: running a job, the run page, Results, and Edit inputs |
| [Desktop and command line](USER_GUIDE.md) | how to install and run the tool on your own computer; **what every control of the desktop window does**; the command line; common jobs; troubleshooting |
| [Input formats](INPUT_FORMATS.md) | the layout of each file you can supply (8,760 profiles, gas monthly profiles, electric and gas ACC), the matching rules, and the CET input issues the tool found |
| [Input fields](INPUT_FIELDS.md) | **every input field**: `Measure.csv`, `ProgramCost.csv`, the run settings, supplied files, reference and built-in tables, with unit and use |
| [Output fields](OUTPUT_FIELDS.md) | **every output field, each with its complete formula in terms of the input fields**: the shared building blocks (quarters, discounting, baseline phasing, prices), then one entry per column with its unit, what each symbol is, the inputs it depends on and a worked number; the other files; two worked examples |
| [Calculations](CALCULATIONS.md) | **the method behind the formulas**: which avoided-cost rows and load shape are used and why, the validation against CET, the deliberate differences and limitations, and a worked example |

Start with the web guide (or, on your own computer, the desktop guide). To trace a number: find its column in Output fields; the entry gives the complete
formula, defines every symbol in it and lists the input fields it uses (described in Input fields), and a worked number you can reproduce. Calculations
explains why the method is what it is.

## How these documents are kept true

They are checked by tests (`tests/test_docs.py`, `test_output_docs.py`, `test_calcs.py`): every column the tool writes, every setting and every control of
the desktop window is described here, the formulas agree with an independent implementation (`tests/reference_calc.py`), and every number and every line
of arithmetic in the worked examples is what a run produces. They are also inside the installed package (`recet/docs`) and open from the desktop window's
**Help > Documentation** menu.
