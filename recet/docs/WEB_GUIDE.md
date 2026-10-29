# Getting started on the web

ReCET prices CET-format energy-efficiency inputs hour by hour on the Avoided Cost Calculator (ACC) and writes CET's outputs for every measure. This page
covers the web site: running a job, reading its results, and editing a small job in the browser. The other documents describe the inputs, the outputs and
how every number is calculated; [Desktop and command line](USER_GUIDE.md) covers the desktop window and the `recet` command.

## 1. Sign in

Accounts are by invitation. An administrator adds you and sends you a setup link, which works once and expires after a few days; on it you choose a
password of at least 10 characters. If you forget your password, ask an administrator for a new setup link. **Your account** (your name, top right)
changes your password.

## 2. Run a job

Open **New run**.

1. **Name** (optional). A blank name becomes the input file's name and the ACC version, for example `my_job · 2026 ACC`.
2. **Inputs.** Either a **job workbook** (.xlsx) with a `Measure` sheet and, optionally, `ProgramCost`, `RunParameters`, `SWAllocation`, `GasProfiles`,
   `Profiles8760` and `Programs` sheets, or **CET files in a .zip**: `Measure.csv` and, optionally, `ProgramCost.csv`, exactly as CET takes them (a CSV on
   its own is not accepted). Each upload has a **template** link beside it. **Optional supplied files** replace the built-in 8,760 electric profiles, gas
   monthly profiles, run parameters, statewide allocation factors or program metadata for this run only; their layouts are in
   [Input formats](INPUT_FORMATS.md). The page states the size limits (150 MB per file, 1,024 MB once unzipped, unless your site set others).
3. **Pricing.** The **ACC version** is named by the year E3 released the calculator ("2026 ACC"); the 2021 and 2022 ACCs are TRC only. **Which ACC
   applies to my filing?** below it is a short table, one row per ACC: the budget filings priced on it and the program years whose claims use it (the ACC
   of the year's final budget filing); ACCs not yet in ReCET are dimmed. **Priced for** says what the run is for (any filing, Quarterly Claims, or one
   budget filing): records priced on an ACC that does not normally apply to their program year are flagged (in the input check and on the run
   page), never stopped. **Compare with** prices the job on a second ACC as well. **First Year**
   is the year everything is discounted to (blank: the earliest claim year). **Market effects** is 0% or 5% on benefits and costs together. The
   **Societal Cost Tests** take about two thirds of a run's time; turn them off when you need only TRC, PAC and RIM. **Split statewide (SW_) programs**
   shares each statewide program across the IOUs with the allocation factors.
4. **Outputs.** CSV (each file zipped), Excel (for jobs of up to 50,000 measures; a larger job writes zipped CSV), or Parquet. **Output template**
   picks the measure-level file's columns and their order: all columns, a built-in template, or one you saved on the **Templates** page. It starts on
   your default template (marked "your default"; set it under **Default for new runs** on the Templates page, or tick **Use for new runs** when you save
   one); a rerun keeps the template of the run it came from. On the Templates page, name the template and pick its columns with the two lists: on the
   left, **Show** chooses input fields, program fields, output fields or one output group, and the search box matches names and definitions; **Add**
   (or double-click, or Enter) moves the selected fields to the template on the right, **Add shown** moves every field listed; on the right, **Top**,
   **Up**, **Down** and **Bottom** (or Alt+Up / Alt+Down) reorder the selected columns and **Remove** (or double-click, or Delete) takes them out. The
   definition of the last field clicked shows below the lists, linked to its entry. **Columns as text** holds the same list one column per line, to
   paste or copy a list. The Results tables, the summary and the key
   metrics still use every column; each run also writes `field_definitions` (each column of the file, defined and linked) and `key_metrics` (the
   headline results and their main drivers by program, sector and end use). Emissions, leaving
   `Exclude_From_CE` programs out of the portfolio summary, and a combined 8,760 electric load profile are options.
5. **CEDARS filing checks.** Quarterly Claims or Budget filing applies CEDARS' own upload checks and flags the records CEDARS would reject or warn on
   (the `filing_validation` table and the input check). Nothing is blocked: the records are still priced.
6. **Start run**, or **Check inputs only** to run just the input check a run makes before pricing (nothing is priced). The upload shows its progress.

Runs wait in a queue and run one at a time (input checks first). A queued run shows its place in the queue and when it should start and finish.

## 3. The run page

The run page opens as soon as the run is queued.

- **Status and progress.** A running run shows a progress bar and the time left; the log below follows it live.
- **Settings.** One line under the title lists the run's settings: measures, First Year, market effects, SCT and emissions on or off, filing checks,
  and the output format.
- **Headline numbers.** A finished run shows its portfolio totals: measures, total system benefit, budget, and the TRC, PAC, RIM and SCT Base ratios.
- **Input check.** What the check before pricing found: problems that stop the run, warnings and notes. Measures the run could not price (no matching
  8,760 profile, an unknown gas profile ...) are listed first, with how many. Every finding, with the rows and cells it names, is in
  `preflight_findings.xlsx`.
- **Files.** The measure-level table, the portfolio summary, `preflight_findings.xlsx`, `run_log.txt` and `inputs.zip` (the files the run was given).
  **Download everything** zips them all.
- **Rerun with different settings** opens New run with this run's files and settings: change a setting (or a file) and start it. Nothing is uploaded
  again.
- **Edit inputs and rerun** copies the run's tables into a new Edit inputs draft (jobs of up to 500 measures).
- **Compare with another run** (a finished run) puts it beside any other finished run of yours, or one shared with you: each level (portfolio, program
  year, segment, sector, program) with both runs' values, the difference (B - A) and the % change, using your Results views' columns and program-year
  choice; **Download this table (CSV)** saves it (BRD BR-06.11). A group found in one run only is blank for the other.
- **Sharing** (your own runs) shares the run read-only with another user by their sign-in email. They find it under **Shared with you** on the runs list
  and can open its results and files, compare it and rerun its inputs as their own run; they cannot stop, delete or share it. **Stop sharing** takes it back.
- **Stop** a queued or running run; **Delete** a finished one. A run and its files are deleted automatically 7 days after it starts (the run page and
  the runs list say when).

**Runs** lists your runs, newest first, with their settings, measures, status, how long they took and when they will be deleted. Expired runs are hidden
until you show them. Queued and running runs update in place. Runs other users shared with you are listed below yours, under **Shared with you**.

## 4. Results

**Results** (on the run page) shows the run's outputs as tables.

- **PA** limits every table to one program administrator. A measure's PA is its credited PA, else its program's PA, else its IOU territory.
- **Program years** (a run with more than one claim year) totals every table, the headline numbers and the chart over the years ticked; none ticked, or
  **all years**, is every year (BRD BR-04.1).
- **Levels:** portfolio, program segment, program sector, program year, program, and measure. Every level above the measure is a sum of its measures; ratios are
  recomputed from the summed benefits and costs. The measure level is shown for runs of up to 50,000 measures, and for up to 5,000 measures of the chosen
  PA; a larger run's measure-level table is in its files.
- The **headline numbers** and a **total system benefit by program sector** chart sit above the table.
- **Columns…** chooses the columns, grouped (ratios, benefits, costs, savings, emissions, societal tests); **Find** narrows the list. **Save view**
  keeps a choice under a name, and **View** switches between your views (Default is built in).
- Click a column heading to **sort**; **Filter rows** keeps the rows containing some text. **Download this table (CSV)** saves the table as shown.
- A run that compared two ACCs has a **Comparison** tab and one tab per ACC.

Every output column, with its formula, is described in [Output fields](OUTPUT_FIELDS.md).

**Email.** When the server has a mail server set up, you get an email when each of your runs finishes or fails, with its headline results and a link
to its page (not for stopped runs or input checks that pass). Turn it off or on under **Account**.

## 5. Edit inputs

**Edit inputs** builds or changes a small job (up to 500 measures) cell by cell. Larger jobs are edited offline, in Excel, and run from New run. Drafts
are kept 30 days after their last change.

- **Start a draft** from the template's example rows, or upload a job workbook or job zip.
- **Editing.** Click a cell to edit it; a cell with ▾ has a list of CET's values to pick from. Select a block and copy or paste it with Excel. Add,
  duplicate and delete rows; undo and redo. Clicking a cell shows what its column means. Column headings are green when the column changes results and
  blue, in italics, when it does not; **Hide no-effect columns** hides the blue ones, and **Go to column** jumps to a column by name.
- **Keyboard.** Tab into the table, move with the arrow keys and press Enter to edit (Escape cancels); Shift+arrows select a block; Ctrl+C / Ctrl+V copy
  and paste; Ctrl+Z / Ctrl+Y undo and redo. The line above the table follows the cell you are on: its column's meaning, what it changes and any problem in
  it. Left / Right switch between the Measure and ProgramCost tabs.
- **Checks.** Every change is saved and checked. The line at the top of the panel gives one verdict: problems that would stop the run (marked ✕, outlined in red),
  cells to check (marked !, amber), or ready to run. **Show the list** lists the cells; click one to go to it, or click or move to a flagged cell to read why
  in the line above the table.
  **Validate inputs** adds the full check a run makes before pricing (profiles, programs, ACC years, and CEDARS filing checks if chosen) on the saved
  tables. Its result says when it is out of date (the tables changed since).
- **Run these inputs** opens New run for the draft. A draft with a problem that would stop the run cannot be run; the page shows the first such cell.
- **Download** the draft as a job workbook or as CET files.

The values offered in the lists are on the [Value lists](value-lists) page.

## 6. Where to read more

| To learn | Read |
|---|---|
| the layout of every file you can supply | [Input formats](INPUT_FORMATS.md) |
| every input field and run setting | [Input fields](INPUT_FIELDS.md) |
| every output column and its formula | [Output fields](OUTPUT_FIELDS.md) |
| the method, the validation against CET, and the deliberate differences | [Calculations](CALCULATIONS.md) |
| the desktop window and the `recet` command | [Desktop and command line](USER_GUIDE.md) |
