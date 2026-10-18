// Smoke test in headless Chromium, at phone and desktop sizes. Loads the
// dashboard against a running server, fails on any page error, and checks
// the banner and the check list actually render. Screenshots land in
// SHOT_DIR (default: the system temp folder).
//
//   MONITOR_DATA_DIR=/tmp/mon-dev python -m uvicorn main:app --port 8050   (in backend/)
//   node ../scripts/smoke_test.js
const path = require("path");
const os = require("os");
let chromium;
try { ({ chromium } = require("playwright")); } catch (e) { ({ chromium } = require("@playwright/test")); }

const BASE = process.env.MONITOR_URL || "http://localhost:8050/";
const SHOT_DIR = process.env.SHOT_DIR || os.tmpdir();

(async () => {
  const launch = {};
  if (process.env.CHROMIUM_PATH) launch.executablePath = process.env.CHROMIUM_PATH;
  const browser = await chromium.launch(launch);
  let failed = false;
  for (const [name, viewport] of [["phone", { width: 390, height: 844 }], ["desktop", { width: 1280, height: 900 }]]) {
    const page = await browser.newPage({ viewport, colorScheme: name === "phone" ? "light" : "dark" });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.getElementById("banner-title").textContent !== "Checking…");
    const title = await page.textContent("#banner-title");
    const rows = await page.locator(".tile").count();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    // The phone layout's whole point is one screen: nothing to scroll to.
    const tall = await page.evaluate(() => document.documentElement.scrollHeight);
    const shot = path.join(SHOT_DIR, `monitor-${name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    console.log(`${name}: banner "${title}", ${rows} tiles, page ${tall}px tall, screenshot ${shot}`);
    if (name === "phone" && tall > viewport.height) { console.error(`phone: needs scrolling (${tall}px > ${viewport.height}px)`); failed = true; }
    if (rows < 5) { console.error(`${name}: expected the tiles to render`); failed = true; }
    // Tap a tile: its sheet must open with the full sentence in it.
    await page.locator(".tile").first().click();
    const sheet = await page.locator("#sheet[open] .sheet-summary").textContent();
    if (!sheet) { console.error(`${name}: tapping a tile opened no detail`); failed = true; }
    await page.locator("#sheet-close").click();
    if (overflow) { console.error(`${name}: page scrolls sideways`); failed = true; }
    if (errors.length) { console.error(`${name}: page errors:\n  ${errors.join("\n  ")}`); failed = true; }
    await page.close();
  }
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
