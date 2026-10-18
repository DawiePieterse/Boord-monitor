// The dashboard. One phone screen: a banner that answers "is the farm all
// right?", then every check as a small tile - a coloured dot, a short name
// and one number. Tap a tile for the whole story and what to do about it.
//
// Built for a phone pulled out of a pocket in the orchard: the colours are
// the answer, readable at a glance without scrolling. The words are one tap
// away for whoever then has to fix it, often by phone call to whoever is at
// the PC.

const STATUS_WORD = { ok: "OK", warn: "Check", fail: "Problem", off: "Off", unknown: "Unknown" };
const STATUS_VALUE = { ok: "ok", warn: "!", fail: "✕", off: "off", unknown: "–" };
const REFRESH_MS = 30000;

let session = { role: "farm", admin: false };
let last = null;
let lastOkAt = null;

const $ = (id) => document.getElementById(id);

// Builds an element without innerHTML: every string shown here comes from a
// check's output, which includes text other programs wrote (log lines,
// Tailscale's health messages).
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// The backend names each tile (`short`, in checks.py next to the labels);
// a report from an older farm has none, so fall back to the full label.
const short = (c) => c.short || c.label;
// A check may name its own state - a device that is merely switched off says
// "Offline", an app in the same grey "Not installed".
const word = (c) => c.word || STATUS_WORD[c.status] || c.status;

function dot(status) {
  return el("span", { class: `dot dot-${status}`, "aria-hidden": "true" });
}

// --- the compact screen ------------------------------------------------------

function tile(c) {
  const value = c.value || STATUS_VALUE[c.status] || "";
  const label = `${c.label}: ${word(c)}. ${c.summary}`;
  // A tile with a percentage is drawn as a meter: a bar says "nearly full"
  // faster than any number can.
  const meter = typeof c.pct === "number";
  const name = el("span", { class: "tile-name" }, short(c));
  const body = meter
    ? [el("span", { class: "tile-top" }, dot(c.status), name),
       el("span", { class: "bar" }, el("span", { class: `bar-fill bar-${c.status}`, style: `width:${Math.max(0, Math.min(100, c.pct))}%` }))]
    : [dot(c.status), name];
  return el("button", { class: `tile${meter ? " tile-meter" : ""} tile-${c.status}`, type: "button", "aria-label": label, onclick: () => showCheck(c) },
    ...body,
    el("span", { class: "tile-value" }, value));
}

function groupCard(title, checks, note) {
  if (!checks.length) return null;
  const worst = checks.some((c) => c.status === "fail") ? "fail"
    : checks.some((c) => c.status === "warn") ? "warn" : "ok";
  return el("section", { class: `card card-${worst}` },
    el("h2", {}, title, note ? el("span", { class: "meta" }, note) : null),
    // A group that runs past one row of five (devices, a long list of Drive
    // folders) gets smaller tiles - six across - so the screen still fits.
    el("div", { class: checks.length > 5 ? "tiles tiles-small" : "tiles" }, checks.map(tile)));
}

// The groups of one report, in the order the backend sends, then any group
// it didn't name (a newer farm talking to an older watcher), titled by key -
// shown rather than silently dropped.
function groups(data) {
  const named = data.groups || [];
  const known = new Set(named.map(([key]) => key));
  const extra = [...new Set(data.checks.map((c) => c.group))].filter((g) => !known.has(g));
  return [...named, ...extra.map((g) => [g, g])];
}

function renderBanner(data) {
  const problems = data.checks.filter((c) => c.status === "fail");
  const warns = data.checks.filter((c) => c.status === "warn");
  $("banner").className = `banner banner-${data.overall}`;
  let title;
  if (data.overall === "fail") title = problems.length === 1 ? "1 problem" : `${problems.length} problems`;
  else if (data.overall === "warn") title = `All up · ${warns.length} to check`;
  else title = data.role === "remote" ? "Farm is reachable" : "All good";
  $("banner-title").textContent = title;
  showUpdated();
  // One line per problem, cut to fit; the whole sentence is a tap away.
  $("banner-problems").replaceChildren(...problems.map((c) =>
    el("button", { class: "problem", type: "button", onclick: () => showCheck(c) },
      el("b", {}, short(c)), el("span", { class: "problem-text" }, c.summary))));
}

let drawn = null;

// "Updated 07:20", and how long ago once that is longer than the monitor
// should ever take. The server checks every minute, so a time three minutes
// old means the monitor itself has stalled - and a stalled monitor is
// exactly the one that can't push to say so. Turning orange is the only way
// it can tell anyone. Run on a timer, not just on render: a stale report is
// by definition one that hasn't changed.
const STALE_MS = 3 * 60 * 1000;

function showUpdated() {
  const at = last && Monitor.parseServerDate(last.checked_at);
  const node = $("checked-at");
  if (!at) { node.textContent = ""; return; }
  const stale = Date.now() - at.getTime() > STALE_MS;
  // One line either way: the screen has no height to spare.
  node.textContent = stale
    ? `Last check ${Monitor.ago(last.checked_at)} - monitor stalled?`
    : `Updated ${Monitor.time(last.checked_at)}`;
  node.classList.toggle("stale", stale);
}

// The number on the Home Screen icon, kept in step whenever the page is
// open. The service worker sets it from each push while the page is not.
function showBadge() {
  if (!last || typeof last.alerting !== "number" || !navigator.setAppBadge) return;
  (last.alerting > 0 ? navigator.setAppBadge(last.alerting) : navigator.clearAppBadge()).catch(() => {});
}

function render() {
  if (!last) return;
  // The page polls every 30 s and the server checks every 60 s, so half the
  // polls bring back the same report. Redrawing it would rebuild every tile
  // for nothing.
  const stamp = `${last.checked_at}|${last.farm ? last.farm.checked_at : ""}`;
  if (stamp === drawn) return;
  drawn = stamp;
  renderBanner(last);
  const cards = [];
  for (const [key, title] of groups(last)) {
    cards.push(groupCard(title, last.checks.filter((c) => c.group === key), null));
  }
  // The remote watcher also carries the farm's own last report, so the one
  // screen shows both what the farm said about itself and what the outside
  // can see now - and when the farm stops answering, the last thing it said.
  if (last.role === "remote" && last.farm && last.farm.checks) {
    const as = Monitor.time(last.farm.checked_at);
    for (const [key, title] of groups(last.farm)) {
      cards.push(groupCard(`Farm said: ${title}`, last.farm.checks.filter((c) => c.group === key), as));
    }
  }
  $("groups").replaceChildren(...cards.filter(Boolean));
}

// --- the sheet ------------------------------------------------------------------

function openSheet(...children) {
  // Absent optional parts arrive as null; replaceChildren would print them.
  $("sheet-body").replaceChildren(...children.filter(Boolean));
  const sheet = $("sheet");
  if (!sheet.open) sheet.showModal();
}

$("sheet-close").addEventListener("click", () => $("sheet").close());
// A tap on the dimmed backdrop closes it too - the dialog element itself is
// the backdrop's click target.
$("sheet").addEventListener("click", (e) => { if (e.target === $("sheet")) $("sheet").close(); });

function showCheck(c) {
  const meta = [];
  if (c.version) meta.push(`v${String(c.version).replace(/^v/, "")}`);
  if (c.tailnet === true) meta.push("reachable over Tailscale");
  if (c.local_ms != null) meta.push(`${c.local_ms} ms on the PC`);
  const actions = [];
  if (c.url && c.status !== "off") {
    actions.push(el("a", { class: "btn btn-quiet", href: c.url, target: "_blank", rel: "noopener" }, "Open app"));
  }
  if (c.app && c.restartable && last && last.role === "farm") {
    actions.push(el("button", { class: "btn", type: "button", onclick: () => { $("sheet").close(); askRestart(c); } }, "Restart"));
  }
  openSheet(
    el("h2", { class: "sheet-title" }, dot(c.status), c.label, el("span", { class: `chip chip-${c.status}` }, word(c))),
    el("p", { class: "sheet-summary" }, c.summary),
    meta.length ? el("p", { class: "muted small" }, meta.join(" · ")) : null,
    c.detail && c.detail.length ? el("pre", { class: "log" }, c.detail.join("\n")) : null,
    actions.length ? el("div", { class: "row-actions" }, actions) : null);
}

$("history-btn").addEventListener("click", () => {
  const events = (last && last.events) || [];
  openSheet(
    el("h2", { class: "sheet-title" }, "Recent changes"),
    events.length
      ? el("ul", { class: "events" }, events.slice(0, 30).map((e) =>
        el("li", {}, el("span", { class: "muted" }, Monitor.time(e.at)), " ",
          dot(e.to), el("b", {}, e.label), " ", STATUS_WORD[e.to] || e.to, ": ", e.summary)))
      : el("p", { class: "muted" }, "Nothing has changed since the monitor started."));
});

// --- loading -----------------------------------------------------------------

async function load(force = false) {
  try {
    last = await Monitor.api(force ? "/api/check" : "/api/status", force ? { method: "POST" } : {});
    lastOkAt = new Date();
    $("offline").hidden = true;
    render();
    showBadge();
  } catch (e) {
    // Keep the last picture on screen, clearly marked as old. A blank page
    // would say less than "this is what it looked like at 04:10".
    $("offline").hidden = false;
    $("offline-since").textContent = lastOkAt
      ? `the picture from ${lastOkAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
      : "nothing yet";
    if (!last) {
      $("banner").className = "banner banner-fail";
      $("banner-title").textContent = "Can't reach the monitor";
    }
  }
}

async function loadSession() {
  try {
    session = await Monitor.api("/api/session");
  } catch (e) { /* keep the defaults; load() reports the outage */ }
  $("role").textContent = session.role === "remote" ? "Off-site watcher" : "Farm PC";
  $("alerts-btn").hidden = session.role !== "farm";
  $("account-label").textContent = session.admin ? "Sign out" : "Sign in as admin";
  const sub = await currentSub().catch(() => null);
  $("alerts-dot").hidden = !sub;
  $("alerts-state").textContent = sub ? "· On" : "· Off";
}

// --- the menu ------------------------------------------------------------------
// Everything that isn't the farm itself lives behind "...", as an iOS
// pull-down menu: the screen stays the answer, and the settings are one tap
// away without a second page to get lost in.

function setMenu(open) {
  $("menu").hidden = !open;
  $("menu-btn").setAttribute("aria-expanded", String(open));
  if (open) $("menu").querySelector(".menu-item:not([hidden])").focus();
}

$("menu-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  setMenu($("menu").hidden);
});
// Choosing an item, tapping anywhere else or Escape closes it.
$("menu").addEventListener("click", () => setMenu(false));
document.addEventListener("click", (e) => {
  if (!$("menu").hidden && !$("menu").contains(e.target)) setMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("menu").hidden) { setMenu(false); $("menu-btn").focus(); }
});

$("account-btn").addEventListener("click", async () => {
  if (session.admin) {
    await Monitor.api("/api/admin/logout", { method: "POST" }).catch(() => {});
    await loadSession();
  } else {
    await needAdmin();
  }
});

// --- PIN -----------------------------------------------------------------------

function askPin() {
  return new Promise((resolve) => {
    const dialog = $("pin-dialog");
    $("pin").value = "";
    $("pin-error").hidden = true;
    const done = (ok) => {
      $("pin-form").onsubmit = null;
      $("pin-cancel").onclick = null;
      dialog.close();
      resolve(ok);
    };
    $("pin-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await Monitor.api("/api/admin/login", { method: "POST", body: { pin: $("pin").value } });
        await loadSession();
        done(true);
      } catch (err) {
        $("pin-error").textContent = err.message;
        $("pin-error").hidden = false;
      }
    };
    $("pin-cancel").onclick = () => done(false);
    dialog.showModal();
    $("pin").focus();
  });
}

async function needAdmin() {
  if (session.admin) return true;
  if (!session.pin_set) {
    openSheet(
      el("h2", { class: "sheet-title" }, "No admin PIN yet"),
      el("p", {}, "No admin PIN has been set on the farm PC yet. Run backend\\set_pin.py there first."));
    return false;
  }
  return askPin();
}

// --- restart -------------------------------------------------------------------

async function askRestart(c) {
  if (!(await needAdmin())) return;
  const dialog = $("restart-dialog");
  $("restart-title").textContent = `Restart ${c.label}?`;
  $("restart-warning").textContent =
    `Everyone using ${c.label} loses it for up to a minute while it restarts. ` +
    "Phones keep what they have scanned and send it once it is back. " +
    "Boord's watchdog does the restart, so it will refuse while an update is running.";
  $("restart-output").hidden = true;
  $("restart-go").hidden = false;
  $("restart-go").disabled = false;
  $("restart-close").textContent = "Cancel";
  $("restart-go").onclick = async () => {
    $("restart-go").disabled = true;
    $("restart-close").textContent = "Close";
    const out = $("restart-output");
    out.hidden = false;
    out.textContent = "Starting…";
    try {
      await Monitor.api(`/api/apps/${encodeURIComponent(c.app)}/restart`, { method: "POST" });
    } catch (err) {
      out.textContent = err.message;
      $("restart-go").disabled = false;
      return;
    }
    $("restart-go").hidden = true;
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      let job;
      try { job = await Monitor.api("/api/restart"); } catch (e) { continue; }
      out.textContent = (job.output || []).join("\n") || "Restarting…";
      if (job.state && job.state !== "running") {
        out.textContent += `\n\n${{ done: "Done.", refused: "Refused - see above.", failed: "Failed - see above." }[job.state] || job.state}`;
        load(true);
        break;
      }
    }
  };
  $("restart-close").onclick = () => dialog.close();
  dialog.showModal();
}

// --- push alerts -----------------------------------------------------------------

function b64ToBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent);
const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

async function currentSub() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

// The alerts panel, in the sheet. Rebuilt after every change so its buttons
// always match what this phone is actually signed up for.
async function showAlerts(note) {
  const n = session.subscribers || 0;
  const phones = `${n} phone${n === 1 ? "" : "s"} signed up`;
  const parts = [el("h2", { class: "sheet-title" }, "Alerts on this phone")];
  const buttons = [];
  if (!pushSupported()) {
    // iPhone Safari only offers Web Push to a site opened from the Home
    // Screen. In an ordinary tab PushManager simply isn't there, and saying
    // "not supported" would send people looking for a different phone.
    parts.push(el("p", {}, isIOS() && !isStandalone()
      ? "To get alerts on an iPhone: tap Share, then Add to Home Screen, and open Monitor from the Home Screen icon. Then come back here."
      : "This browser cannot receive alerts."));
  } else {
    const sub = await currentSub().catch(() => null);
    if (sub) {
      parts.push(el("p", {}, `On. This phone is told when something breaks and when it is fixed. (${phones}.)`));
      buttons.push(el("button", { class: "btn", type: "button", onclick: sendTest }, "Send a test"));
      buttons.push(el("button", { class: "btn btn-quiet", type: "button", onclick: alertsOff }, "Turn off"));
    } else if (Notification.permission === "denied") {
      parts.push(el("p", {}, "Notifications are blocked for this app. Allow them in Settings → Notifications → Monitor, then reopen it."));
    } else {
      parts.push(el("p", {}, `Off on this phone. (${phones}.)`));
      buttons.push(el("button", { class: "btn", type: "button", onclick: alertsOn }, "Turn on alerts"));
    }
  }
  if (note) parts.push(el("p", { class: "muted" }, note));
  if (session.admin) buttons.push(el("button", { class: "btn btn-quiet", type: "button", onclick: signOut }, "Sign out"));
  parts.push(el("div", { class: "row-actions" }, buttons));
  openSheet(...parts);
}

$("alerts-btn").addEventListener("click", () => showAlerts());

async function alertsOn() {
  // The permission request has to be the first thing this tap does. iOS only
  // honours it as part of the user's gesture, and a PIN dialog awaited first
  // uses the gesture up - the request is then refused without ever showing.
  // So signing in is its own tap.
  if (!session.admin) {
    $("sheet").close();
    if (await needAdmin()) await showAlerts("Signed in. Now tap Turn on alerts again.");
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { await showAlerts(); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(session.push_key) });
    const label = `${isIOS() ? "iPhone" : "Phone"}, added ${new Date().toLocaleDateString([], { day: "numeric", month: "short" })}`;
    await Monitor.api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label } });
    await loadSession();
    await showAlerts();
  } catch (e) {
    await showAlerts(`Could not turn on alerts: ${e.message}`);
  }
}

async function sendTest() {
  if (!session.admin) {
    $("sheet").close();
    if (!(await needAdmin())) return;
  }
  try {
    const r = await Monitor.api("/api/push/test", { method: "POST" });
    await showAlerts(`Test sent to ${r.sent} of ${r.subscribers} phone(s).` + (r.failed ? ` ${r.failed} failed.` : ""));
  } catch (e) {
    await showAlerts(e.message);
  }
}

async function alertsOff() {
  const sub = await currentSub();
  if (sub) {
    await Monitor.api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } }).catch(() => {});
    await sub.unsubscribe();
  }
  await loadSession();
  await showAlerts();
}

async function signOut() {
  await Monitor.api("/api/admin/logout", { method: "POST" });
  await loadSession();
  await showAlerts();
}

// --- start -----------------------------------------------------------------------

async function checkNow() {
  const btn = $("check-now");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("spinning");
  await load(true);
  btn.disabled = false;
  btn.classList.remove("spinning");
}

$("check-now").addEventListener("click", checkNow);

// Pull down to check now, as in every iPhone list. The page never scrolls,
// so any downward drag from the top is a pull. Not while a sheet or the
// menu is open - there a drag means something else.
const PULL_PX = 70;
let pullFrom = null;
let pulled = 0;

function setPull(px) {
  pulled = px;
  const ptr = $("ptr");
  ptr.style.transform = `translateY(${Math.min(px, PULL_PX + 20) - 40}px) rotate(${px * 4}deg)`;
  ptr.style.opacity = String(Math.min(1, px / PULL_PX));
  ptr.classList.toggle("ready", px >= PULL_PX);
}

document.addEventListener("touchstart", (e) => {
  const busy = document.querySelector("dialog[open]") || !$("menu").hidden;
  pullFrom = !busy && window.scrollY <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null;
}, { passive: true });

document.addEventListener("touchmove", (e) => {
  if (pullFrom === null) return;
  // Halved, so the indicator lags the finger as iOS's does.
  setPull(Math.max(0, (e.touches[0].clientY - pullFrom) / 2));
}, { passive: true });

document.addEventListener("touchend", () => {
  if (pullFrom === null) return;
  pullFrom = null;
  const go = pulled >= PULL_PX;
  $("ptr").classList.add("settle");
  setPull(0);
  setTimeout(() => $("ptr").classList.remove("settle"), 250);
  if (go) checkNow();
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}

$("version").textContent = `v${Monitor.VERSION}`;
loadSession();
load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
setInterval(showUpdated, 15000);
