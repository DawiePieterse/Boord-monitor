// The dashboard. Four tabs, the way status pages and monitoring tools lay
// things out, sized for a phone first:
//
//   Status    - is the farm all right? The overall state in Statuspage's
//               words, any open incident, the headline numbers (availability,
//               MTTR, error budget) and 90 days of uptime per component.
//   Services  - one card per app: its SLO, how much error budget is left,
//               how fast it is burning, latency percentiles, a 24-hour chart.
//   Incidents - the incident log with timelines, MTTA/MTTR/MTBF, maintenance
//               windows, and every raw status change.
//   Checks    - every check as a small tile: the old one-screen glance.
//
// The colours are still the answer at a glance; the numbers are for looking
// back - which app needs work before next season - and the words are one
// tap away in the sheet for whoever has to fix it.

const STATUS_WORD = { ok: "OK", warn: "Check", fail: "Problem", off: "Off", unknown: "Unknown" };
const STATUS_VALUE = { ok: "ok", warn: "!", fail: "✕", off: "off", unknown: "–" };
const SLO_WORD = { met: "Met", at_risk: "At risk", missed: "Missed", nodata: "No data" };
const SLO_CLASS = { met: "ok", at_risk: "warn", missed: "fail", nodata: "off" };
const REFRESH_MS = 30000;
// How long the Restart dialog follows a restart before giving up on it. See
// askRestart for why this is a bit over twice the watchdog's 150 s.
const RESTART_WAIT_MS = 5 * 60 * 1000;
const VIEWS = ["status", "services", "incidents", "checks"];

let session = { role: "farm", admin: false };
let last = null;          // /api/status
let overview = null;      // /api/overview
let incidentLog = null;   // /api/incidents
let lastOkAt = null;
let statWindow = "30d";
const historyCache = {};  // check id -> {stamp, data}

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

function chip(status, text) {
  return el("span", { class: `chip chip-${status}` }, text);
}

function card(title, ...children) {
  return el("section", { class: "card" }, title ? el("h2", {}, ...[].concat(title)) : null, ...children);
}

const checkById = (id) => (last && last.checks.find((c) => c.id === id)) || null;

// --- tabs ------------------------------------------------------------------------
// The hash is the tab, so the iOS back gesture and a notification's link
// both land somewhere sensible, and a reload keeps the tab.

function currentView() {
  const v = (location.hash || "").replace("#", "");
  return VIEWS.includes(v) ? v : "status";
}

function showView() {
  const v = currentView();
  for (const name of VIEWS) $(`view-${name}`).hidden = name !== v;
  for (const t of document.querySelectorAll(".tab")) {
    if (t.dataset.view === v) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  }
  $("nav-title").textContent = $(`view-${v}`).dataset.title;
  window.scrollTo(0, 0);
  renderView();
  if (v === "incidents") loadIncidents();
  if (v === "services") loadServiceCharts();
}

window.addEventListener("hashchange", showView);

function renderView() {
  if (!last) return;
  showIncidentBadge();
  const v = currentView();
  if (v === "status") renderStatus();
  else if (v === "services") renderServices();
  else if (v === "incidents") renderIncidents();
  else renderChecks();
}

// --- status ------------------------------------------------------------------------

// Statuspage's words for the overall state, from what the apps are doing:
// any app down is a partial outage, every app down a major one. A failed
// check that is not an app (a backup, a key) is still a problem, but the
// apps are answering, so it reads as degraded, not as an outage.
function overallWords(data) {
  const fails = data.checks.filter((c) => c.status === "fail");
  const appChecks = data.checks.filter((c) => (c.group === "apps" || c.id.startsWith("remote.")) && c.status !== "off");
  const appsDown = appChecks.filter((c) => c.status === "fail");
  if (data.role === "remote") {
    const farm = data.checks.find((c) => c.id === "farm.monitor");
    if (farm && farm.status === "fail") return ["fail", "Farm PC unreachable"];
  }
  if (appsDown.length && appsDown.length === appChecks.length) return ["fail", "Major outage"];
  if (appsDown.length) return ["fail", "Partial outage"];
  if (fails.length) return ["fail", fails.length === 1 ? "1 problem" : `${fails.length} problems`];
  if (data.overall === "warn") return ["warn", "Degraded · " + data.checks.filter((c) => c.status === "warn").length + " to check"];
  return ["ok", data.role === "remote" ? "Farm is reachable" : "All systems operational"];
}

function renderBanner(data) {
  const [cls, title] = overallWords(data);
  $("banner").className = `banner banner-${cls}`;
  $("banner-title").textContent = title;
  showUpdated();
  const problems = data.checks.filter((c) => c.status === "fail");
  $("banner-problems").replaceChildren(...problems.map((c) =>
    el("button", { class: "problem", type: "button", onclick: () => showCheck(c) },
      el("b", {}, short(c)), el("span", { class: "problem-text" }, c.summary))));
}

function incidentRow(i) {
  const started = Monitor.parseServerDate(i.started_at);
  const dur = (i.resolved_at ? Monitor.parseServerDate(i.resolved_at) : new Date()) - started;
  const state = { open: ["fail", "Open"], acknowledged: ["warn", "Acknowledged"], resolved: ["ok", "Resolved"] }[i.state] || ["off", i.state];
  return el("button", { class: "incident", type: "button", onclick: () => showIncident(i.id) },
    el("span", { class: "incident-top" },
      dot(state[0]), el("b", {}, i.label), chip(state[0], state[1]),
      i.paged ? null : el("span", { class: "muted small" }, "not paged")),
    el("span", { class: "incident-summary" }, i.summary),
    el("span", { class: "muted small" },
      `Started ${Monitor.time(i.started_at)} · ${i.resolved_at ? "lasted" : "for"} ${Charts.duration(dur / 1000)}`
      + (i.acked_by ? ` · ack: ${i.acked_by}` : "")));
}

function renderActiveIncidents() {
  const list = (last && last.incidents) || [];
  $("active-incidents").replaceChildren(list.length
    ? card([`Active incidents`, el("span", { class: "meta" }, String(list.length))], el("div", { class: "incident-list" }, list.map(incidentRow)))
    : "");
}

// The count on the Incidents tab. Set from every render, whichever tab is
// open: it used to be set only with the Status tab, so a phone left on
// Checks or Services showed no badge for an incident that opened meanwhile.
function showIncidentBadge() {
  const n = ((last && last.incidents) || []).length;
  const badge = $("incident-badge");
  badge.hidden = !n;
  badge.textContent = String(n);
}

function silenceText(s) {
  const what = s.match === "*" ? "Everything" : s.match.startsWith("group:") ? groupTitle(s.match.slice(6)) : labelOf(s.match);
  return `${what} until ${Monitor.time(s.ends_at)} - ${s.reason}`;
}

function renderActiveSilences() {
  const list = (last && last.silences) || [];
  $("active-silences").replaceChildren(...list.map((s) =>
    el("div", { class: "notice notice-maint" },
      el("b", {}, "Maintenance: "), silenceText(s), ". No alerts are sent for it.")));
}

function kpi(label, value, sub, status) {
  return el("div", { class: `kpi${status ? " kpi-" + status : ""}` },
    el("span", { class: "kpi-label" }, label),
    el("span", { class: "kpi-value" }, value),
    sub ? el("span", { class: "kpi-sub" }, sub) : null);
}

function renderKpis() {
  if (!overview || !last || last.role !== "farm") { $("kpis").replaceChildren(); return; }
  const k = overview.kpi;
  const st = k.incidents_30d;
  const budget = k.error_budget_min;
  $("kpis").replaceChildren(
    kpi("Availability", Charts.pct(k.availability_30d), "apps, 30 days",
      k.availability_30d === null ? null : k.availability_30d >= 0.995 ? "ok" : k.availability_30d >= 0.99 ? "warn" : "fail"),
    kpi("Open incidents", String(k.incidents_open), `${st.count} in 30 days`, k.incidents_open ? "fail" : "ok"),
    kpi("MTTR", Charts.duration(st.mttr_s), "mean time to resolve"),
    kpi("Error budget", budget === null ? "–" : budget < 0 ? "Spent" : Charts.pct(budget, 0), "lowest app left",
      budget === null ? null : budget < 0 ? "fail" : budget < 0.25 ? "warn" : "ok"));
}

function groupTitle(key) {
  const g = last && (last.groups || []).find(([k]) => k === key);
  return g ? g[1] : key;
}

function labelOf(id) {
  const c = checkById(id);
  return c ? c.label : id;
}

function componentRow(c) {
  const note = el("span", { class: "comp-note muted small" });
  const bars = Charts.uptimeBars(c.days, (d) => {
    note.textContent = d.availability === null
      ? `${d.day}: no data`
      : `${d.day}: ${Charts.pct(d.availability)} up` + (d.fail ? `, ${d.fail} failed check${d.fail === 1 ? "" : "s"}` : "");
  });
  return el("div", { class: "comp" },
    el("button", { class: "comp-head", type: "button", onclick: () => { const x = checkById(c.id); if (x) showCheck(x); } },
      dot(c.status), el("span", { class: "comp-name" }, c.label),
      el("span", { class: "comp-pct" }, Charts.pct(c.availability))),
    bars, note);
}

function renderComponents() {
  if (!overview) { $("components").replaceChildren(); return; }
  const byGroup = {};
  for (const c of overview.components) (byGroup[c.group] = byGroup[c.group] || []).push(c);
  const cards = [];
  for (const [key, title] of groups(last)) {
    const list = byGroup[key];
    if (!list) continue;
    cards.push(card([title, el("span", { class: "meta" }, `${overview.days} days`)],
      el("div", { class: "comps" }, list.map(componentRow)),
      el("div", { class: "bars-legend muted small" }, el("span", {}, `${overview.days} days ago`), el("span", {}, "Today"))));
  }
  $("components").replaceChildren(...cards);
}

function renderStatus() {
  renderBanner(last);
  renderActiveIncidents();
  renderActiveSilences();
  renderKpis();
  renderComponents();
}

// --- services ---------------------------------------------------------------------

function sloCard(s) {
  const c = checkById(s.check_id) || { status: "unknown", label: s.name };
  const lat = s.latency;
  const budget = s.error_budget;
  const rem = budget.remaining;
  const left = rem === null ? null : Math.max(0, rem) * budget.allowed_s;
  const chartBox = el("div", { class: "chart-box", "data-check": s.check_id },
    chartFor(s.check_id, "ms", lat.objective_ms));
  const actions = [];
  if (c.url && c.status !== "off") actions.push(el("a", { class: "btn btn-quiet", href: c.url, target: "_blank", rel: "noopener" }, "Open"));
  if (c.app && c.restartable && last.role === "farm") actions.push(el("button", { class: "btn btn-quiet", type: "button", onclick: () => askRestart(c) }, "Restart"));
  actions.push(el("button", { class: "btn btn-quiet", type: "button", onclick: () => showCheck(c) }, "Details"));

  return el("section", { class: `card svc svc-${c.status}` },
    el("div", { class: "svc-head" },
      dot(c.status), el("h2", {}, s.name),
      c.version ? el("span", { class: "muted small" }, `v${String(c.version).replace(/^v/, "")}`) : null,
      el("span", { class: "svc-now" }, c.status === "fail" ? word(c) : Charts.ms(c.local_ms))),
    el("div", { class: "svc-slo" },
      el("div", {},
        el("span", { class: "muted small" }, `SLO ${s.target}% · ${s.window_days} days`),
        el("span", { class: "svc-big" }, Charts.pct(s.availability["30d"]))),
      chip(SLO_CLASS[s.status], SLO_WORD[s.status])),
    el("div", { class: "svc-budget" },
      el("div", { class: "row-between small" },
        el("span", {}, "Error budget"),
        el("span", { class: "muted" }, rem === null ? "no data yet" : rem < 0
          ? `over by ${Charts.duration(budget.spent_s - budget.allowed_s)}`
          : `${Charts.pct(rem, 0)} left · ${Charts.duration(left)} of ${Charts.duration(budget.allowed_s)}`)),
      Charts.meter(rem, rem === null ? "" : rem < 0.25 ? "meter-fail" : rem < 0.5 ? "meter-warn" : "meter-ok")),
    el("dl", { class: "stats" },
      stat("24 h", Charts.pct(s.availability["24h"])),
      stat("7 days", Charts.pct(s.availability["7d"])),
      stat("Burn 1 h", Charts.burn(s.burn_rate["1h"]), burnClass(s.burn_rate["1h"])),
      stat("Burn 6 h", Charts.burn(s.burn_rate["6h"]), burnClass(s.burn_rate["6h"])),
      stat("p50", Charts.ms(lat["24h"].p50)),
      stat("p95", Charts.ms(lat["24h"].p95), lat["24h"].p95 !== null && lat["24h"].p95 > lat.objective_ms ? "warn" : null),
      stat("p99", Charts.ms(lat["24h"].p99)),
      stat("p95 7 d", Charts.ms(lat["7d"].p95), lat.met === false ? "warn" : null)),
    el("div", { class: "row-between small muted chart-caption" },
      el("span", {}, "Answer time, last 24 h"),
      el("span", {}, `objective p95 < ${Charts.ms(lat.objective_ms)}`)),
    chartBox,
    s.coverage !== null && s.coverage < 0.9
      ? el("p", { class: "muted small" }, `The monitor watched ${Charts.pct(s.coverage, 0)} of these ${s.window_days} days; the figures cover only that.`)
      : null,
    el("div", { class: "row-actions" }, actions));
}

function stat(label, value, cls) {
  return el("div", { class: `stat${cls ? " stat-" + cls : ""}` }, el("dt", {}, label), el("dd", {}, value));
}

function burnClass(v) {
  if (v === null || v === undefined) return null;
  return v >= 14.4 ? "fail" : v >= 6 ? "warn" : null;
}

function chartFor(checkId, key, objective) {
  const h = historyCache[checkId];
  if (!h) return el("div", { class: "chart-empty muted small" }, "Loading…");
  if (!h.data.points.length) return el("div", { class: "chart-empty muted small" }, "No readings yet.");
  const to = Date.now(), from = to - h.data.hours * 3600000;
  return Charts.line(h.data.points, key, { objective, from, to, unit: key === "ms" ? " ms" : "%" });
}

async function loadHistory(checkId, hours = 24) {
  const stamp = last ? last.checked_at : "";
  const hit = historyCache[checkId];
  if (hit && hit.stamp === stamp && hit.data.hours === hours) return hit.data;
  const data = await Monitor.api(`/api/history/${encodeURIComponent(checkId)}?hours=${hours}`);
  historyCache[checkId] = { stamp, data };
  return data;
}

// The charts load after the cards, one request per app, so the numbers are
// on screen at once and the lines fill in.
async function loadServiceCharts() {
  if (!overview || currentView() !== "services") return;
  await Promise.all(overview.slo.map(async (s) => {
    try {
      await loadHistory(s.check_id);
    } catch (e) { return; }
    const box = document.querySelector(`.chart-box[data-check="${CSS.escape(s.check_id)}"]`);
    if (box) box.replaceChildren(chartFor(s.check_id, "ms", s.latency.objective_ms));
  }));
}

function renderServices() {
  if (last.role !== "farm") {
    $("slo-note").textContent = "Service levels are kept by the monitor on the farm PC. This watcher sees each app from outside:";
    $("services").replaceChildren(...last.checks.filter((c) => c.id.startsWith("remote.")).map((c) =>
      el("section", { class: `card svc svc-${c.status}` },
        el("div", { class: "svc-head" }, dot(c.status), el("h2", {}, c.label), el("span", { class: "svc-now" }, c.value || word(c))),
        el("p", { class: "small" }, c.summary))));
    return;
  }
  $("slo-note").textContent = "Availability is the share of the monitor's checks each app answered. "
    + "The error budget is the downtime its objective allows; burn rate is how fast that is being spent (1× is on pace).";
  if (!overview) { $("services").replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  if (!overview.slo.length) { $("services").replaceChildren(el("p", { class: "muted" }, "No apps installed on this PC.")); return; }
  $("services").replaceChildren(...overview.slo.map(sloCard));
}

// --- incidents ---------------------------------------------------------------------

async function loadIncidents() {
  try {
    incidentLog = await Monitor.api("/api/incidents?limit=100");
  } catch (e) { /* keep the last one; load() shows the outage */ }
  if (currentView() === "incidents") renderIncidents();
}

function renderIncidents() {
  for (const b of $("stat-window").querySelectorAll("button")) b.setAttribute("aria-selected", String(b.dataset.w === statWindow));
  if (!incidentLog) { $("incident-list").replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  const st = incidentLog.stats[statWindow];
  $("incident-stats").replaceChildren(
    kpi("Incidents", String(st.count), st.open ? `${st.open} still open` : "all resolved", st.open ? "fail" : null),
    kpi("MTTR", Charts.duration(st.mttr_s), "mean time to resolve"),
    kpi("MTTA", Charts.duration(st.mtta_s), "mean time to acknowledge"),
    kpi("MTBF", Charts.duration(st.mtbf_s), "mean time between failures"),
    kpi("Downtime", Charts.duration(st.downtime_s), "total, resolved incidents"));

  const sil = incidentLog.silences || [];
  $("silence-list").replaceChildren(card(["Maintenance windows",
    el("button", { class: "text-btn head-btn", type: "button", onclick: showMaintenance }, "New")],
  sil.length
    ? el("ul", { class: "plain-list" }, sil.map((s) => el("li", {},
      el("span", {}, silenceText(s)),
      el("button", { class: "text-btn", type: "button", onclick: () => endSilence(s.id) }, "End"))))
    : el("p", { class: "muted small" }, "None. Set one before working on the PC, so the phones aren't woken for it.")));

  const list = incidentLog.incidents;
  $("incident-list").replaceChildren(card("Incident log", list.length
    ? el("div", { class: "incident-list" }, list.map(incidentRow))
    : el("p", { class: "muted small" }, "No incidents yet. One opens when a check fails three times in a row.")));

  const events = (last && last.events) || [];
  $("change-list").replaceChildren(card("Recent changes", events.length
    ? el("ul", { class: "events" }, events.slice(0, 40).map((e) =>
      el("li", {}, el("span", { class: "muted" }, Monitor.time(e.at)), " ",
        dot(e.to), el("b", {}, e.label), " ", STATUS_WORD[e.to] || e.to, ": ", e.summary)))
    : el("p", { class: "muted small" }, "Nothing has changed since the monitor started.")));
}

$("stat-window").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  statWindow = b.dataset.w;
  renderIncidents();
});

async function showIncident(id) {
  let inc;
  try {
    inc = await Monitor.api(`/api/incidents/${id}`);
  } catch (e) {
    openSheet(el("p", {}, Monitor.errorText(e)));
    return;
  }
  const state = { open: "fail", acknowledged: "warn", resolved: "ok" }[inc.state] || "off";
  const actions = [];
  if (inc.state === "open") actions.push(el("button", { class: "btn", type: "button", onclick: () => ackIncident(inc.id) }, "Acknowledge"));
  const c = checkById(inc.check_id);
  if (c) actions.push(el("button", { class: "btn btn-quiet", type: "button", onclick: () => showCheck(c) }, "The check now"));
  const end = inc.resolved_at ? Monitor.parseServerDate(inc.resolved_at) : new Date();
  openSheet(
    el("h2", { class: "sheet-title" }, dot(state), inc.label, chip(state, inc.state[0].toUpperCase() + inc.state.slice(1))),
    el("p", { class: "sheet-summary" }, inc.summary),
    el("p", { class: "muted small" }, `Incident #${inc.id} · ${inc.severity} · `
      + `${inc.resolved_at ? "lasted" : "open for"} ${Charts.duration((end - Monitor.parseServerDate(inc.started_at)) / 1000)}`
      + (inc.paged ? " · phones were told" : " · no phone was woken")),
    el("ol", { class: "timeline" }, inc.timeline.map((e) =>
      el("li", { class: `tl tl-${e.kind}` },
        el("span", { class: "tl-time muted small" }, Monitor.time(e.at)),
        el("span", { class: "tl-kind" }, e.kind[0].toUpperCase() + e.kind.slice(1)),
        el("span", { class: "tl-text small" }, e.text)))),
    actions.length ? el("div", { class: "row-actions" }, actions) : null);
}

async function ackIncident(id) {
  $("sheet").close();
  if (!(await needAdmin())) return;
  try {
    await Monitor.api(`/api/incidents/${id}/ack`, { method: "POST", body: { by: deviceName() } });
  } catch (e) {
    openSheet(el("p", {}, Monitor.errorText(e)));
    return;
  }
  await load();
  await loadIncidents();
  showIncident(id);
}

// Who acknowledged, as well as a shared PIN can say: the phone's own label,
// remembered on it. There are no accounts on the farm.
function deviceName() {
  try {
    let n = localStorage.getItem("monitor_name");
    if (!n) {
      n = (prompt("Your name, so the incident says who is on it:") || "").trim().slice(0, 40);
      if (n) localStorage.setItem("monitor_name", n);
    }
    return n || (isIOS() ? "iPhone" : "phone");
  } catch (e) {
    return "phone";
  }
}

// --- maintenance windows ---------------------------------------------------------------

function showMaintenance() {
  if (!last) return;
  const what = el("select", { id: "maint-what" },
    el("option", { value: "*" }, "Everything"),
    ...(last.groups || []).map(([k, t]) => el("option", { value: `group:${k}` }, `All of ${t}`)),
    ...last.checks.filter((c) => c.group === "apps" && c.status !== "off").map((c) => el("option", { value: c.id }, c.label)));
  const howLong = el("select", { id: "maint-minutes" },
    ...[[30, "30 minutes"], [60, "1 hour"], [120, "2 hours"], [240, "4 hours"], [480, "8 hours"], [1440, "24 hours"]]
      .map(([m, t]) => el("option", { value: String(m), selected: m === 60 }, t)));
  const reason = el("input", { id: "maint-reason", type: "text", maxlength: "120", placeholder: "e.g. Installing updates" });
  const err = el("p", { class: "error small", hidden: true });
  openSheet(
    el("h2", { class: "sheet-title" }, "Maintenance window"),
    el("p", { class: "muted small" }, "While it lasts the dashboard still shows what is broken, but no phone is woken for it. "
      + "If it is still broken when the window ends, the phones are told then."),
    el("label", { class: "field" }, el("span", {}, "What"), what),
    el("label", { class: "field" }, el("span", {}, "For"), howLong),
    el("label", { class: "field" }, el("span", {}, "Why"), reason),
    err,
    el("div", { class: "row-actions" },
      el("button", { class: "btn", type: "button", onclick: async (ev) => {
        if (!session.admin) { $("sheet").close(); if (!(await needAdmin())) return; }
        // Disabled while the request is out: a second tap on a slow
        // connection made a second, identical window, each needing its
        // own End.
        const btn = ev.currentTarget;
        btn.disabled = true;
        try {
          await Monitor.api("/api/silences", { method: "POST",
            body: { match: what.value, minutes: Number(howLong.value), reason: reason.value || "maintenance" } });
        } catch (e) {
          btn.disabled = false;
          if (!$("sheet").open) openSheet(err);
          err.textContent = Monitor.errorText(e);
          err.hidden = false;
          return;
        }
        $("sheet").close();
        await load(true);
        await loadIncidents();
      } }, "Start")));
}

async function endSilence(id) {
  if (!(await needAdmin())) return;
  try {
    await Monitor.api(`/api/silences/${id}/end`, { method: "POST" });
  } catch (e) { /* already ended; the reload shows it */ }
  await load(true);
  await loadIncidents();
}

$("maint-btn").addEventListener("click", showMaintenance);

// --- checks ------------------------------------------------------------------------

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

function renderChecks() {
  const cards = [];
  for (const [key, title] of groups(last)) {
    cards.push(groupCard(title, last.checks.filter((c) => c.group === key), null));
  }
  // The remote watcher also carries the farm's own last report, so one
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
  if (c.alert === false) meta.push("never wakes a phone");
  const actions = [];
  if (c.url && c.status !== "off") {
    actions.push(el("a", { class: "btn btn-quiet", href: c.url, target: "_blank", rel: "noopener" }, "Open app"));
  }
  if (c.app && c.restartable && last && last.role === "farm") {
    actions.push(el("button", { class: "btn", type: "button", onclick: () => { $("sheet").close(); askRestart(c); } }, "Restart"));
  }
  // The last day of this check: a line where it has a number (answer time,
  // how full), and its status strip either way.
  const key = c.local_ms != null ? "ms" : typeof c.pct === "number" ? "num" : null;
  const chartBox = el("div", { class: "chart-box" }, el("div", { class: "chart-empty muted small" }, "Loading the last 24 hours…"));
  openSheet(
    el("h2", { class: "sheet-title" }, dot(c.status), c.label, chip(c.status, word(c))),
    el("p", { class: "sheet-summary" }, c.summary),
    meta.length ? el("p", { class: "muted small" }, meta.join(" · ")) : null,
    el("div", { class: "row-between small muted chart-caption" },
      el("span", {}, key === "ms" ? "Answer time, last 24 h" : key === "num" ? "% used, last 24 h" : "Status, last 24 h"),
      el("span", {}, "now")),
    chartBox,
    c.detail && c.detail.length ? el("pre", { class: "log" }, c.detail.join("\n")) : null,
    actions.length ? el("div", { class: "row-actions" }, actions) : null);
  loadHistory(c.id).then((data) => {
    if (!data.points.length) {
      chartBox.replaceChildren(el("div", { class: "chart-empty muted small" }, "No history yet."));
      return;
    }
    const to = Date.now();
    chartBox.replaceChildren(Charts.line(data.points, key || "none", { from: to - 86400000, to, height: key ? 64 : 12 }));
    if (key === "ms" && data.latency.n) {
      chartBox.append(el("p", { class: "muted small" },
        `p50 ${Charts.ms(data.latency.p50)} · p95 ${Charts.ms(data.latency.p95)} · p99 ${Charts.ms(data.latency.p99)}`));
    }
  }).catch(() => chartBox.replaceChildren(el("div", { class: "chart-empty muted small" }, "History not available.")));
}

// --- the header's updated time ------------------------------------------------------

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

// --- loading -----------------------------------------------------------------

let drawn = null;

async function load(force = false) {
  try {
    last = await Monitor.api(force ? "/api/check" : "/api/status", force ? { method: "POST" } : {});
    lastOkAt = new Date();
    $("offline").hidden = true;
    showBadge();
  } catch (e) {
    // Keep the last picture on screen, clearly marked as old. A blank page
    // would say less than "this is what it looked like at 04:10".
    $("offline").hidden = false;
    $("offline-since").textContent = lastOkAt
      ? `the picture from ${Monitor.hm(lastOkAt)}`
      : "nothing yet";
    if (!last) {
      $("banner").className = "banner banner-fail";
      $("banner-title").textContent = "Can't reach the monitor";
    }
    return;
  }
  // The page polls every 30 s and the server checks every 60 s, so half the
  // polls bring back the same report; only a new one is redrawn and pulls
  // the overview, which changes only with a new run.
  const stamp = `${last.checked_at}|${last.farm ? last.farm.checked_at : ""}|${(last.silences || []).length}|${(last.incidents || []).map((i) => i.state).join()}`;
  if (stamp === drawn && overview) return;
  drawn = stamp;
  renderView();
  try {
    overview = await Monitor.api("/api/overview");
  } catch (e) { /* history is extra; the live view is already drawn */ }
  renderView();
  if (currentView() === "services") loadServiceCharts();
  if (currentView() === "incidents") loadIncidents();
}

async function loadSession() {
  try {
    session = await Monitor.api("/api/session");
  } catch (e) { /* keep the defaults; load() reports the outage */ }
  $("role").textContent = session.role === "remote" ? "Off-site watcher" : "Farm PC";
  $("alerts-btn").hidden = session.role !== "farm";
  $("maint-btn").hidden = session.role !== "farm";
  $("account-label").textContent = session.admin ? "Sign out" : "Sign in as admin";
  const sub = await currentSub().catch(() => null);
  $("alerts-dot").hidden = !sub;
  $("alerts-state").textContent = sub ? "· On" : "· Off";
}

// --- the menu ------------------------------------------------------------------

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

// Resolves true once signed in, and null when the person backs out - by
// Cancel, or by Escape, which closes a modal <dialog> through its own
// "cancel" event without touching our buttons. Callers treat anything but
// true as "cancelled".
function askPin() {
  return new Promise((resolve) => {
    const dialog = $("pin-dialog");
    $("pin").value = "";
    $("pin-error").hidden = true;
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      $("pin-form").onsubmit = null;
      $("pin-cancel").onclick = null;
      dialog.oncancel = null;
      dialog.onclose = null;
      if (dialog.open) dialog.close();
      resolve(ok);
    };
    dialog.oncancel = () => done(null);
    dialog.onclose = () => done(null);
    $("pin-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await Monitor.api("/api/admin/login", { method: "POST", body: { pin: $("pin").value } });
        await loadSession();
        done(true);
      } catch (err) {
        $("pin-error").textContent = Monitor.errorText(err);
        $("pin-error").hidden = false;
      }
    };
    $("pin-cancel").onclick = () => done(null);
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
  return (await askPin()) === true;
}

// --- restart -------------------------------------------------------------------

let restartRun = 0;

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
      out.textContent = Monitor.errorText(err);
      $("restart-go").disabled = false;
      return;
    }
    $("restart-go").hidden = true;
    // Follow the job until it ends - but not forever. The watchdog's own
    // restart is given 150 s (restart.py) before the monitor calls it
    // failed, so five minutes is past anything a real restart takes. It
    // also stops when the dialog is closed and on any 4xx; a dropped
    // connection (5xx, timeout) is retried, because the PC is busy
    // restarting things and may be slow to answer.
    const deadline = Date.now() + RESTART_WAIT_MS;
    const run = ++restartRun;
    const watching = () => dialog.open && run === restartRun;
    let job = null;
    while (watching()) {
      await new Promise((r) => setTimeout(r, 2000));
      if (!watching()) return;
      if (Date.now() > deadline) {
        out.textContent += "\n\nStill not finished after 5 minutes. Close this and look at the app's tile - " +
          "the watchdog log on the farm PC has the rest.";
        return;
      }
      try {
        job = await Monitor.api("/api/restart");
      } catch (e) {
        if (e.status >= 400 && e.status < 500) {
          out.textContent += `\n\n${e.status === 401 ? "Signed out - sign in again to follow the restart." : Monitor.errorText(e)}`;
          if (e.status === 401) loadSession();
          return;
        }
        continue;
      }
      if (!watching()) return;
      out.textContent = (job.output || []).join("\n") || "Restarting…";
      if (job.state && job.state !== "running") {
        out.textContent += `\n\n${{ done: "Done.", refused: "Refused - see above.", failed: "Failed - see above." }[job.state] || job.state}`;
        load(true);
        return;
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
  // uses the gesture up. So signing in is its own tap.
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
    const label = `${isIOS() ? "iPhone" : "Phone"}, added ${Monitor.day(new Date())}`;
    await Monitor.api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label } });
    await loadSession();
    await showAlerts();
  } catch (e) {
    await showAlerts(`Could not turn on alerts: ${Monitor.errorText(e)}`);
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
    await showAlerts(Monitor.errorText(e));
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
  await Monitor.api("/api/admin/logout", { method: "POST" }).catch(() => {});
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

// Pull down to check now, as in every iPhone list. Only from the very top
// of the page, and not while a sheet or the menu is open - there a drag
// means something else.
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
  if (window.scrollY > 0) { pullFrom = null; setPull(0); return; }
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
showView();
loadSession();
load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
setInterval(showUpdated, 15000);
