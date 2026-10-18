// The dashboard. One screen: a banner that answers "is the farm all right?"
// at a glance, then every check underneath for when it is not.
//
// Built for a phone pulled out of a pocket in the orchard: the answer is the
// banner's colour, readable without glasses or a signal good enough to load
// anything else. Everything below it is for the person who then has to fix
// it, often by phone call to whoever is at the PC.

const GROUPS = {
  farm: [
    ["apps", "Apps"],
    ["tailscale", "Tailscale"],
    ["pc", "Farm PC"],
    ["guards", "Safety nets"],
    ["backups", "Google Drive backups"],
  ],
  remote: [
    ["farm", "The farm, seen from outside"],
    ["watcher", "This watcher"],
  ],
};

const STATUS_WORD = { ok: "OK", warn: "Check", fail: "Problem", off: "Not installed", unknown: "Unknown" };
const REFRESH_MS = 30000;

let session = { role: "farm", admin: false };
let last = null;
let lastOkAt = null;
const open = new Set(); // check ids whose detail is expanded, kept across refreshes

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

function dot(status) {
  return el("span", { class: `dot dot-${status}`, "aria-label": STATUS_WORD[status] || status });
}

// --- rendering ---------------------------------------------------------------

function renderBanner(data) {
  const banner = $("banner");
  const problems = data.checks.filter((c) => c.status === "fail");
  const warns = data.checks.filter((c) => c.status === "warn");
  banner.className = `banner banner-${data.overall}`;
  let title;
  if (data.overall === "fail") {
    title = problems.length === 1 ? `${problems[0].label}: problem` : `${problems.length} problems`;
  } else if (data.overall === "warn") {
    title = `All up · ${warns.length} to check`;
  } else {
    title = data.role === "remote" ? "Farm is reachable" : "All good";
  }
  $("banner-title").textContent = title;
  $("checked-at").textContent = `Checked ${Monitor.time(data.checked_at)}`;
  const list = $("banner-problems");
  list.replaceChildren(...problems.map((c) => el("li", {}, el("b", {}, c.label), " - ", c.summary)));
}

function checkRow(c) {
  const isOpen = open.has(c.id);
  const actions = [];
  if (c.url && c.status !== "off") {
    actions.push(el("a", { class: "pill", href: c.url, target: "_blank", rel: "noopener" }, "Open"));
  }
  if (c.app && c.restartable && session.role === "farm" && last && last.role === "farm") {
    actions.push(el("button", { class: "pill", type: "button", onclick: (e) => { e.stopPropagation(); askRestart(c); } }, "Restart"));
  }
  const meta = [];
  if (c.version) meta.push(`v${String(c.version).replace(/^v/, "")}`);
  if (c.tailnet === true) meta.push("Tailscale OK");
  const hasDetail = c.detail && c.detail.length;
  return el("li", { class: `check check-${c.status}` },
    el("div", { class: "check-head", role: hasDetail ? "button" : null, tabindex: hasDetail ? "0" : null,
      onclick: hasDetail ? () => { isOpen ? open.delete(c.id) : open.add(c.id); render(); } : null },
      dot(c.status),
      el("div", { class: "check-body" },
        el("div", { class: "check-label" }, c.label,
          meta.length ? el("span", { class: "meta" }, meta.join(" · ")) : null),
        el("div", { class: "check-summary" }, c.summary)),
      hasDetail ? el("span", { class: "chev" }, isOpen ? "▾" : "▸") : null),
    actions.length ? el("div", { class: "check-actions" }, actions) : null,
    hasDetail && isOpen ? el("pre", { class: "log" }, c.detail.join("\n")) : null);
}

function groupCard(title, checks, note) {
  if (!checks.length) return null;
  const worst = checks.some((c) => c.status === "fail") ? "fail"
    : checks.some((c) => c.status === "warn") ? "warn" : "ok";
  return el("section", { class: "card" },
    el("h2", {}, dot(worst), title, note ? el("span", { class: "meta" }, note) : null),
    el("ul", { class: "checks" }, checks.map(checkRow)));
}

function render() {
  if (!last) return;
  renderBanner(last);
  const cards = [];
  for (const [key, title] of GROUPS[last.role]) {
    cards.push(groupCard(title, last.checks.filter((c) => c.group === key)));
  }
  // The remote watcher also carries the farm's own last report, so the one
  // screen shows both what the farm said about itself and what the outside
  // can see now - and when the farm stops answering, the last thing it said.
  if (last.role === "remote" && last.farm && last.farm.checks) {
    const as = `as of ${Monitor.time(last.farm.checked_at)}`;
    for (const [key, title] of GROUPS.farm) {
      cards.push(groupCard(`Farm PC's report: ${title}`, last.farm.checks.filter((c) => c.group === key), as));
    }
  }
  $("groups").replaceChildren(...cards.filter(Boolean));

  const events = last.events || [];
  $("events-card").hidden = !events.length;
  $("events").replaceChildren(...events.slice(0, 15).map((e) =>
    el("li", {}, el("span", { class: "muted" }, Monitor.time(e.at)), " ",
      dot(e.to), el("b", {}, e.label), " ", STATUS_WORD[e.to] || e.to, ": ", e.summary)));
}

// --- loading -----------------------------------------------------------------

async function load(force = false) {
  try {
    last = await Monitor.api(force ? "/api/check" : "/api/status", force ? { method: "POST" } : {});
    lastOkAt = new Date();
    $("offline").hidden = true;
    render();
  } catch (e) {
    // Keep the last picture on screen, clearly marked as old. A blank page
    // would say less than "this is what it looked like at 04:10".
    $("offline").hidden = false;
    $("offline-since").textContent = lastOkAt ? lastOkAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "earlier";
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
  $("account").hidden = session.role !== "farm";
  $("account").textContent = session.admin ? "Sign out" : "Sign in";
  $("alerts-card").hidden = session.role !== "farm";
  await renderAlerts();
}

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
    alert("No admin PIN has been set on the farm PC yet. Run backend\\set_pin.py there first.");
    return false;
  }
  return askPin();
}

$("account").addEventListener("click", async () => {
  if (session.admin) {
    await Monitor.api("/api/admin/logout", { method: "POST" });
    await loadSession();
    render();
  } else if (await needAdmin()) {
    render();
  }
});

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

async function renderAlerts() {
  if (session.role !== "farm") return;
  const state = $("alerts-state");
  const [on, test, off] = [$("alerts-on"), $("alerts-test"), $("alerts-off")];
  on.hidden = test.hidden = off.hidden = true;
  if (!pushSupported()) {
    // iPhone Safari only offers Web Push to a site opened from the Home
    // Screen. In an ordinary tab PushManager simply isn't there, and saying
    // "not supported" would send people looking for a different phone.
    state.textContent = isIOS() && !isStandalone()
      ? "To get alerts on an iPhone: tap Share, then Add to Home Screen, and open Monitor from the Home Screen icon. Then come back here."
      : "This browser cannot receive alerts.";
    return;
  }
  const sub = await currentSub().catch(() => null);
  const n = session.subscribers || 0;
  if (sub) {
    state.textContent = `On. This phone is told when something breaks and when it is fixed. (${n} phone${n === 1 ? "" : "s"} signed up.)`;
    test.hidden = off.hidden = false;
  } else if (Notification.permission === "denied") {
    state.textContent = "Notifications are blocked for this app. Allow them in Settings → Notifications → Monitor, then reopen it.";
  } else {
    state.textContent = `Off on this phone. (${n} phone${n === 1 ? "" : "s"} signed up.)`;
    on.hidden = false;
  }
}

$("alerts-on").addEventListener("click", async () => {
  // The permission request has to be the first thing this tap does. iOS only
  // honours it as part of the user's gesture, and a PIN dialog awaited first
  // uses the gesture up - the request is then refused without ever showing.
  // So signing in is its own tap.
  if (!session.admin) {
    if (await needAdmin()) $("alerts-state").textContent = "Signed in. Now tap Turn on alerts again.";
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { await renderAlerts(); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(session.push_key) });
    const label = `${isIOS() ? "iPhone" : "Phone"}, added ${new Date().toLocaleDateString([], { day: "numeric", month: "short" })}`;
    await Monitor.api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label } });
    await loadSession();
  } catch (e) {
    alert(`Could not turn on alerts: ${e.message}`);
  }
});

$("alerts-test").addEventListener("click", async () => {
  if (!(await needAdmin())) return;
  try {
    const r = await Monitor.api("/api/push/test", { method: "POST" });
    $("alerts-state").textContent = `Test sent to ${r.sent} of ${r.subscribers} phone(s).` + (r.failed ? ` ${r.failed} failed.` : "");
  } catch (e) {
    alert(e.message);
  }
});

$("alerts-off").addEventListener("click", async () => {
  const sub = await currentSub();
  if (sub) {
    await Monitor.api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } }).catch(() => {});
    await sub.unsubscribe();
  }
  await loadSession();
});

// --- start -----------------------------------------------------------------------

$("check-now").addEventListener("click", async () => {
  $("check-now").disabled = true;
  $("check-now").textContent = "Checking…";
  await load(true);
  $("check-now").disabled = false;
  $("check-now").textContent = "Check now";
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}

$("version").textContent = `Boord Monitor ${Monitor.VERSION}`;
loadSession();
load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
