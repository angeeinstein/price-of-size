// Browser smoke test of the control panel against a running coordinator.
//   BASE_URL=http://localhost:8000 PANEL_USER=admin PANEL_PASSWORD=... node e2e/smoke.mjs
// Logs in (after a deliberately wrong password), opens every page, makes one change
// through the UI (CSRF), reloads a deep link, toggles dark mode, and fails on any
// unexpected console error. Screenshots go to SHOTS_DIR if set.
import { chromium } from "playwright";

const base = process.env.BASE_URL ?? "http://localhost:8000";
const user = process.env.PANEL_USER ?? "admin";
const password = process.env.PANEL_PASSWORD;
const shots = process.env.SHOTS_DIR;
if (!password) throw new Error("set PANEL_PASSWORD");

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1360, height: 860 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => {
  // 401s are expected: the session check before login and the wrong password
  if (m.type() === "error" && !m.text().includes("401")) errors.push(m.text());
});
const shot = async (name) => shots && page.screenshot({ path: `${shots}/${name}.png` });

try {
  await page.goto(`${base}/`);
  await page.fill('input[name="username"]', user);
  await page.fill('input[name="password"]', "definitely-not-the-password");
  await page.click('button[type="submit"]');
  await page.waitForSelector('[role="alert"]');
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForSelector("text=Nodes online");
  await shot("overview");

  const pages = {
    nodes: "Nodes", jobs: "Jobs and queue", domains: "Domains", sources: "Sources",
    explorer: "Explorer", releases: "Releases", settings: "Settings", users: "Users",
    account: "Account",
  };
  for (const [p, title] of Object.entries(pages)) {
    await page.click(`a[href="/${p}"]`);
    // the page itself rendered (a crash would leave the previous one on screen)
    await page.getByRole("heading", { level: 1, name: title, exact: true }).waitFor();
    await page.waitForLoadState("networkidle");
    await shot(p);
  }

  await page.click('a[href="/jobs"]');
  await page.fill('textarea[aria-label="URLs"]', `https://smoke.example.invalid/p/${Date.now()}`);
  await page.getByRole("button", { name: "Queue", exact: true }).click();
  await page.waitForSelector("text=1 queued");

  // the settings form: a number saved through the form is there after a reload
  await page.click('a[href="/settings"]');
  const rate = () => page.getByLabel("Start rate per domain", { exact: true });
  await rate().fill("0.3");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.waitForSelector("text=default: 0.2 req/s");
  await page.reload();
  await rate().waitFor();
  if ((await rate().inputValue()) !== "0.3") throw new Error("the saved rate did not stick");
  await shot("settings-form");
  await page.getByText("reset", { exact: true }).first().click();
  await page.waitForSelector("text=default: 0.2 req/s", { state: "detached" });

  await page.goto(`${base}/domains`);
  await page.waitForSelector("text=smoke.example.invalid");
  await page.click('button[aria-label="Toggle dark mode"]');
  await shot("domains-dark");

  if (errors.length) throw new Error(`console errors:\n${errors.join("\n")}`);
  console.log("PANEL SMOKE TEST PASSED");
} finally {
  await browser.close();
}
