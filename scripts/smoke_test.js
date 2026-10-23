// Smoke test in headless Chromium, at phone and desktop sizes. Loads the
// dashboard against a running server, opens every tab, fails on any page
// error or sideways scrolling, and checks each tab actually draws.
// Screenshots land in SHOT_DIR (default: the system temp folder).
//
// The server in CI watches a farm with nothing installed, so it shows few
// checks. The "phone-busy" pass answers /api/status from
// fixtures/busy_farm.json instead - two problems and sixteen devices - so
// the layout is proven on a real farm's worth of checks too.
//
// Since 2.0 the page scrolls below the fold (history, incidents). What must
// still hold on a phone: the answer - the status banner - is on screen
// without scrolling, above the tab bar.
//
//   MONITOR_DATA_DIR=/tmp/mon-dev python -m uvicorn main:app --port 8050   (in backend/)
//   node ../scripts/smoke_test.js
const path = require("path");
const os = require("os");
let chromium;
try { ({ chromium } = require("playwright")); } catch (e) { ({ chromium } = require("@playwright/test")); }

const BASE = process.env.MONITOR_URL || "http://localhost:8050/";
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();
const BUSY = require(path.join(__dirname, "fixtures", "busy_farm.json"));

(async () => {
  const launch = {};
  if (process.env.CHROMIUM_PATH) launch.executablePath = process.env.CHROMIUM_PATH;
  const browser = await chromium.launch(launch);
  let failed = false;
  const fail = (msg) => { console.error(msg); failed = true; };
  const phone = { width: 390, height: 844 };
  for (const [name, viewport] of [["phone", phone], ["phone-busy", phone], ["desktop", { width: 1280, height: 900 }]]) {
    const page = await browser.newPage({ viewport, colorScheme: name === "desktop" ? "dark" : "light" });
    if (name === "phone-busy") {
      // Fresh timestamp, or the page would (rightly) say the monitor has
      // stopped checking. Naive UTC, as the server sends it.
      const body = JSON.stringify({ ...BUSY, checked_at: new Date().toISOString().slice(0, 19) });
      await page.route(/\/api\/(status|check)$/, (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body }));
    }
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.getElementById("banner-title").textContent !== "Checking…");

    // Status tab: the banner answers, above the tab bar, without scrolling.
    const title = await page.textContent("#banner-title");
    const bannerBottom = await page.evaluate(() => document.getElementById("banner").getBoundingClientRect().bottom);
    const tabTop = await page.evaluate(() => document.querySelector(".tabbar").getBoundingClientRect().top);
    if (bannerBottom > tabTop) fail(`${name}: the status banner is hidden behind the tab bar`);
    await page.locator("#components .comp").first().waitFor({ timeout: 5000 }).catch(() => fail(`${name}: no uptime bars`));
    const comps = await page.locator("#components .comp").count();
    await page.screenshot({ path: path.join(SHOT_DIR, `monitor-${name}-status.png`), fullPage: true });

    // Checks tab: every check as a tile; tapping one opens its sheet.
    await page.locator('.tab[data-view="checks"]').click();
    // The tab switches on hashchange, which fires after the click returns.
    await page.locator("#view-checks .tile").first().waitFor({ timeout: 5000 }).catch(() => {});
    const tiles = await page.locator("#view-checks .tile").count();
    if (tiles < 5) fail(`${name}: expected the tiles to render, got ${tiles}`);
    await page.locator("#view-checks .tile").first().click();
    const sheet = await page.locator("#sheet[open] .sheet-summary").textContent();
    if (!sheet) fail(`${name}: tapping a tile opened no detail`);
    await page.locator("#sheet[open] .chart-box svg, #sheet[open] .chart-empty").first().waitFor({ timeout: 5000 })
      .catch(() => fail(`${name}: the check's sheet drew no chart`));
    await page.locator("#sheet-close").click();
    await page.screenshot({ path: path.join(SHOT_DIR, `monitor-${name}-checks.png`), fullPage: true });

    // Services tab: a card per app with its SLO, once the overview is in.
    await page.locator('.tab[data-view="services"]').click();
    await page.locator("#services .svc").first().waitFor({ timeout: 5000 }).catch(() => {});
    const services = await page.locator("#services .svc").count();
    if (name !== "phone-busy" && services < 1) fail(`${name}: no service cards`);
    await page.screenshot({ path: path.join(SHOT_DIR, `monitor-${name}-services.png`), fullPage: true });

    // Incidents tab: stats, maintenance windows, the log.
    await page.locator('.tab[data-view="incidents"]').click();
    await page.locator("#incident-stats .kpi").first().waitFor({ timeout: 5000 }).catch(() => fail(`${name}: no incident stats`));
    await page.locator('#stat-window button[data-w="7d"]').click();
    await page.screenshot({ path: path.join(SHOT_DIR, `monitor-${name}-incidents.png`), fullPage: true });

    // The "..." menu opens and closes.
    await page.locator("#menu-btn").click();
    if (!(await page.locator("#menu").isVisible())) fail(`${name}: the menu did not open`);
    await page.keyboard.press("Escape");
    if (await page.locator("#menu").isVisible()) fail(`${name}: the menu stayed open`);

    for (const v of ["status", "services", "incidents", "checks"]) {
      await page.locator(`.tab[data-view="${v}"]`).click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      if (overflow) fail(`${name}: the ${v} tab scrolls sideways`);
    }
    console.log(`${name}: banner "${title}", ${comps} components, ${tiles} tiles, ${services} services, screenshots in ${SHOT_DIR}`);
    if (errors.length) fail(`${name}: page errors:\n  ${errors.join("\n  ")}`);
    await page.close();
  }
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
