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

  for (const p of ["nodes", "jobs", "domains", "sources", "releases", "settings", "users", "account"]) {
    await page.click(`a[href="/${p}"]`);
    await page.waitForLoadState("networkidle");
    await shot(p);
  }

  await page.click('a[href="/jobs"]');
  await page.fill('textarea[aria-label="URLs"]', `https://smoke.example.invalid/p/${Date.now()}`);
  await page.getByRole("button", { name: "Queue", exact: true }).click();
  await page.waitForSelector("text=1 queued");

  await page.goto(`${base}/domains`);
  await page.waitForSelector("text=smoke.example.invalid");
  await page.click('button[aria-label="Toggle dark mode"]');
  await shot("domains-dark");

  if (errors.length) throw new Error(`console errors:\n${errors.join("\n")}`);
  console.log("PANEL SMOKE TEST PASSED");
} finally {
  await browser.close();
}
