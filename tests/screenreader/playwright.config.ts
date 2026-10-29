// NVDA walk-through of the ReCET web app. Windows only: run `npm run setup-nvda` once (Guidepup's portable NVDA), then `npm test`.
// withGuidepup runs the tests one at a time in a headed browser, as NVDA needs the browser window in front.
import { devices } from "@playwright/test";
import { withGuidepup } from "@guidepup/playwright";
import path from "node:path";

const repo = path.resolve(__dirname, "..", "..");

export default withGuidepup({
  testDir: ".",
  reportSlowTests: null,
  timeout: 5 * 60 * 1000,
  retries: 0,
  projects: [{ name: "chrome", use: { ...devices["Desktop Chrome"], channel: "chrome", viewport: { width: 1280, height: 900 } } }],
  webServer: {
    command: `${process.env.RECET_PYTHON ?? "python"} tests/screenreader/serve_a11y.py`,
    cwd: repo,
    url: "http://127.0.0.1:8765/ReCET/login",
    timeout: 120 * 1000,
    reuseExistingServer: false,
  },
});
