// NVDA walk-through of the ReCET web app's main pages. It records what NVDA says after each key press, with the element that has the focus,
// to results/<flow>.json; it asserts only that each page loads. Read the transcripts against ARCHITECTURE 1.4 / 8.7 (WCAG 2.2 AA).
// Nothing here starts a run or changes a value: the only form sent is Results' Apply (a GET) and Edit inputs' Validate inputs (a check).
import { nvdaTest as test, type NVDAPlaywright as NVDA } from "@guidepup/playwright";
import { NVDAKeyCodeCommands as K, WindowsKeyCodes, WindowsModifiers } from "@guidepup/guidepup";
import type { Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const OUT = path.join(__dirname, "results");
const server = () => JSON.parse(fs.readFileSync(path.join(__dirname, ".server.json"), "utf-8"));

/** Bring the Chrome window titled `title` to the front (focusWindow.ps1). Returns "True", "False" or "notfound". */
const raise = (title: string) => execFileSync("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(__dirname, "focusWindow.ps1")],
  { env: { ...process.env, RECET_WINDOW_TITLE: title }, encoding: "utf-8" }).trim();

const clean = (s: string) => s.toLowerCase().replace(/[|¦:;'"`\-‐–—·_()[\]{}\\^~]/g, " ").replace(/\s+/g, " ").trim();
const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Guidepup's navigateToWebContent, with a sturdier first step. Guidepup finds the browser by pressing Alt+Esc (next window) up to 10 times
 *  until NVDA reports its title, which fails on a desktop with more windows than that. Here the Chrome window is first brought to the front
 *  by its title (focusWindow.ps1), and Alt+Esc is the fallback, up to 60 times. The rest is Guidepup 0.20's own sequence. */
async function toWebContent(nvda: NVDA, page: Page) {
  await nvda.perform(K.exitFocusMode, { capture: false });
  const title = await page.title();
  let heard = "";
  const inBrowser = async () => {
    await nvda.perform(K.reportTitle);
    heard = await nvda.lastSpokenPhrase();
    // Spaces are ignored: NVDA speaks "ReCET" as "Re CET".
    return clean(heard).replace(/ /g, "").includes(clean(title).replace(/ /g, ""));
  };
  const focus = () => raise(title);
  const ps = focus();
  await pause(500);
  let tries = 0;
  while (!(await inBrowser())) {
    if (++tries > 60) throw new Error(`NVDA never reached the Chrome window "${title}" (focusWindow.ps1: ${ps}; NVDA last said: "${heard}").`);
    // Chrome is already in front when focusWindow.ps1 succeeded, so Alt+Esc would only move it away: raise it again instead.
    if (ps === "True") focus();
    else await nvda.perform({ keyCode: [WindowsKeyCodes.Escape], modifiers: [WindowsModifiers.Alt] }, { capture: false });
    await pause(500);
  }
  await page.bringToFront();
  await page.locator("body").focus();
  await page.locator("body").click();
  await page.locator("body").blur();
  for (const c of [K.readNextFocusableItem, K.toggleBetweenBrowseAndFocusMode, K.toggleBetweenBrowseAndFocusMode, K.exitFocusMode]) {
    await nvda.perform(c, { capture: false });
    await pause(100);
  }
  await nvda.clearSpokenPhraseLog();
  await nvda.clearItemTextLog();
  await nvda.perform({ keyCode: [WindowsKeyCodes.Home], modifiers: [WindowsModifiers.Control] });
}

type Entry = { step: string; spoken: string[]; focus: string; url: string; refocused?: boolean; live?: Record<string, string> };

class Transcript {
  entries: Entry[] = [];
  seen = 0;
  constructor(private name: string, private page: Page, private nvda: NVDA) {}

  /** Run one step (a key, an NVDA command, or a Playwright action) and record what NVDA said since the last step. */
  async step(label: string, action: () => Promise<unknown>, settleMs = 400, live: string[] = []) {
    // A key sent while another window has the foreground goes to that window (a first run typed into Windows Search): raise Chrome first.
    const refocused = !(await this.page.evaluate(() => document.hasFocus()).catch(() => true));
    if (refocused) { raise(await this.page.title()); await this.page.waitForTimeout(500); }
    await action();
    await this.page.waitForTimeout(settleMs);
    const log = await this.nvda.spokenPhraseLog();
    const focus = await this.page.evaluate(() => {
      const e = document.activeElement as HTMLElement | null;
      if (!e || e === document.body) return "(body)";
      const id = e.id ? `#${e.id}` : "";
      const cls = typeof e.className === "string" && e.className ? `.${e.className.trim().split(/\s+/).join(".")}` : "";
      const name = e.getAttribute("aria-label") ?? (e.innerText || (e as HTMLInputElement).value || "").trim().slice(0, 60);
      return `${e.tagName.toLowerCase()}${id}${cls}${e.getAttribute("role") ? `[role=${e.getAttribute("role")}]` : ""} "${name}"`;
    }).catch(() => "(navigating)");
    const liveText: Record<string, string> = {};
    for (const sel of live) liveText[sel] = await this.page.locator(sel).first().innerText().catch(() => "(missing)");
    this.entries.push({ step: label, spoken: log.slice(this.seen), focus, url: this.page.url(),
      ...(refocused ? { refocused } : {}), ...(live.length ? { live: liveText } : {}) });
    this.seen = log.length;
  }

  async keys(key: string, times = 1, settleMs = 400) {
    for (let i = 1; i <= times; i++) await this.step(times > 1 ? `${key} (${i})` : key, () => this.nvda.press(key), settleMs);
  }

  async cmd(name: keyof NVDA["keyboardCommands"], times = 1) {
    if (String(name).startsWith("moveToNext") && !["moveToNextColumn", "moveToNextRow"].includes(String(name)))
      await this.nvda.perform(this.nvda.keyboardCommands.exitFocusMode, { capture: false });
    for (let i = 1; i <= times; i++) await this.step(times > 1 ? `${String(name)} (${i})` : String(name), () => this.nvda.perform(this.nvda.keyboardCommands[name]));
  }

  async start() {
    await this.page.bringToFront();
    await this.step("to web content (Ctrl+Home)", () => toWebContent(this.nvda, this.page), 800);
  }

  save() {
    fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, `${this.name}.json`), JSON.stringify(this.entries, null, 1));
  }
}

async function signIn(page: Page) {
  const s = server();
  await page.goto(`${s.base}/login`, { waitUntil: "load" });
  await page.fill("input[name=email]", s.email);
  await page.fill("input[name=password]", s.password);
  await Promise.all([page.waitForNavigation(), page.click("button.primary")]);
}

async function open(page: Page, url: string, ready: string) {
  await page.goto(url, { waitUntil: "load" });
  await page.locator(ready).first().waitFor();
}

test.describe.configure({ mode: "serial" });

test("1 sign in page", async ({ page, nvda }) => {
  const t = new Transcript("1_login", page, nvda);
  await open(page, `${server().base}/login`, "form");
  await t.start();
  await t.cmd("moveToNextHeading", 2);
  await t.keys("Tab", 4);
  t.save();
});

test("2 runs list", async ({ page, nvda }) => {
  const t = new Transcript("2_runs", page, nvda);
  await signIn(page);
  await open(page, `${server().base}/runs`, "main");
  await t.start();
  await t.keys("Tab");          // the skip link
  await t.keys("Enter", 1, 800);
  await t.cmd("moveToNextLandmark", 3);
  await t.cmd("moveToNextHeading", 5);
  await t.cmd("moveToNextTable");
  await t.cmd("moveToNextColumn", 2);
  await t.cmd("moveToNextRow", 2);
  await t.keys("Tab", 8);
  t.save();
});

test("3 new run", async ({ page, nvda }) => {
  const t = new Transcript("3_new_run", page, nvda);
  await signIn(page);
  await open(page, `${server().base}/runs/new`, "form");
  await t.start();
  await t.cmd("moveToNextHeading", 6);
  await t.keys("Tab", 34);      // through the whole form; Enter is never pressed
  t.save();
});

test("4 run page", async ({ page, nvda }) => {
  const t = new Transcript("4_run", page, nvda);
  await signIn(page);
  await open(page, server().run, "main h1");
  await t.start();
  await t.cmd("moveToNextHeading", 8);
  await t.keys("Tab", 16);      // stops on Stop / Delete / Share but never presses them
  await t.step("focus run log", () => page.locator("pre[aria-label='Run log']").focus());
  await t.cmd("moveToNext", 3);
  t.save();
});

test("5 results", async ({ page, nvda }) => {
  const t = new Transcript("5_results", page, nvda);
  await signIn(page);
  await open(page, server().results, "#results-table");
  await t.start();
  await t.cmd("moveToNextHeading", 4);
  await t.step("focus 'What these numbers are'", () => page.locator("summary", { hasText: "What these numbers are" }).first().focus());
  await t.keys("Enter");
  await t.cmd("moveToNext", 3);
  await t.step("focus PA select", () => page.locator("select[name=pa]").focus());
  await t.keys("ArrowDown", 1, 1000); // must not reload the page (3.2.2): compare the url with the next entry's
  await t.step("focus Apply", () => page.locator("#filters button", { hasText: "Apply" }).focus());
  await t.keys("Enter", 1, 2000);
  await page.locator("#results-table").waitFor();
  await t.step("focus first sort button", () => page.locator("#results-table .sort-btn").first().focus());
  await t.keys("Enter", 1, 800);
  await t.keys("Enter", 1, 800);
  await t.cmd("moveToNextTable");
  await t.cmd("moveToNextColumn", 3);
  await t.cmd("moveToNextRow", 2);
  await t.step("focus row filter", () => page.locator("input[data-filter='results-table']").focus());
  await t.step("type 'Po'", () => nvda.type("Po"), 2500, ["#filter-count"]);   // the row count is a role=status line
  await t.keys("Tab");
  t.save();
});

test("6 edit inputs grid", async ({ page, nvda }) => {
  const t = new Transcript("6_edit", page, nvda);
  await signIn(page);
  await open(page, server().edit, ".tabulator-cell");
  await page.waitForTimeout(1500);   // the cell checks run after the grid is built
  await t.start();
  await t.cmd("moveToNextHeading", 3);
  await t.step("focus Measure tab", () => page.locator("#tab-Measure").focus());
  await t.keys("ArrowRight", 1, 1200);
  await t.keys("ArrowLeft", 1, 1200);
  await t.keys("Tab", 3, 600);
  await t.step("focus the Measure grid", () => page.locator("#grid-Measure .tabulator-tableholder").focus(), 800, ["#help"]);
  for (const k of ["ArrowDown", "ArrowDown", "ArrowRight", "ArrowRight", "ArrowRight"]) await t.step(k, () => nvda.press(k), 800, ["#help"]);
  await t.step("click the EUL_Yrs cell of T3 (negative life)",
    () => page.locator(".tabulator-row").nth(2).locator(".tabulator-cell[tabulator-field='EUL_Yrs']").click(), 1000, ["#help"]);
  await t.step("Escape (leave the cell editor)", () => nvda.press("Escape"), 800, ["#help"]);
  await t.step("ArrowLeft", () => nvda.press("ArrowLeft"), 800, ["#help"]);
  await t.step("ArrowRight (back to EUL_Yrs)", () => nvda.press("ArrowRight"), 800, ["#help"]);
  await t.step("focus help line", () => page.locator("#help").focus());
  await t.step("focus Validate inputs", () => page.locator("#check-btn").focus());
  await t.step("Enter (Validate inputs)", () => nvda.press("Enter"), 10000, ["#issue-summary", "#check-result"]);
  await t.step("focus check result", () => page.locator("#check-result pre, #check-result").first().focus(), 800);
  await t.cmd("moveToNext", 2);
  t.save();
});
