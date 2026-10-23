// Charts and number formatting for the dashboard, as inline SVG. No chart
// library: the phones are tailnet-only (no CDNs), and the three kinds of
// chart this needs - uptime bars, a line with its status strip, a meter -
// are a few dozen lines each.
//
// Every chart is drawn in a fixed viewBox and stretched to its box's width
// (preserveAspectRatio="none"), so it fills a phone or a desktop card alike.
// Colours come from the stylesheet's status classes, so dark mode needs
// nothing here.

const Charts = {
  NS: "http://www.w3.org/2000/svg",

  svg(tag, attrs = {}, ...children) {
    const node = document.createElementNS(Charts.NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      node.setAttribute(k, String(v));
    }
    for (const c of children.flat()) if (c) node.append(c);
    return node;
  },

  // --- numbers, as a status page writes them ------------------------------------

  // Availability to as many places as tell nines apart: 99.95%, not 100%.
  // 100% is only written when nothing failed at all.
  pct(ratio, places) {
    if (ratio === null || ratio === undefined) return "–";
    const p = ratio * 100;
    if (p >= 100) return "100%";
    const d = places !== undefined ? places : p >= 99.9 ? 3 : p >= 99 ? 2 : 1;
    // Never round up into a nine the app didn't earn: 99.996 is 99.99%.
    const f = Math.pow(10, d);
    return `${(Math.floor(p * f) / f).toFixed(d)}%`;
  },

  ms(v) {
    if (v === null || v === undefined) return "–";
    return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`;
  },

  // 3 h 36 min, 12 min, 40 s, 2 d 4 h - the two biggest units only.
  duration(s) {
    if (s === null || s === undefined) return "–";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s} s`;
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return h ? `${d} d ${h} h` : `${d} d`;
    if (h) return m ? `${h} h ${m} min` : `${h} h`;
    return `${m} min`;
  },

  burn(v) {
    if (v === null || v === undefined) return "–";
    return v < 0.05 ? "0×" : `${v < 10 ? v.toFixed(1) : Math.round(v)}×`;
  },

  // The colour of a day's bar, Statuspage's way: green when nothing failed,
  // amber for a short outage, red for a real one, grey for no data.
  dayClass(a) {
    if (a === null || a === undefined) return "nodata";
    if (a >= 0.9999) return "ok";
    if (a >= 0.99) return "warn";
    return "fail";
  },

  // --- uptime bars -------------------------------------------------------------

  // One thin bar per day, oldest on the left. `onPick(day)` is called with
  // the tapped day so the page can say what happened on it; a bar is too
  // thin to carry its own label on a phone.
  uptimeBars(days, onPick) {
    const n = days.length || 1;
    const gap = n > 60 ? 0.6 : 1.2;
    const w = (300 - gap * (n - 1)) / n;
    const g = Charts.svg("svg", { viewBox: "0 0 300 28", preserveAspectRatio: "none", class: "bars",
      role: "img", "aria-label": `Daily availability, last ${n} days` });
    days.forEach((d, i) => {
      const r = Charts.svg("rect", { x: (i * (w + gap)).toFixed(2), y: 0, width: w.toFixed(2), height: 28, rx: Math.min(1.5, w / 2),
        class: `bar-day day-${Charts.dayClass(d.availability)}` });
      r.append(Charts.svg("title", {}, document.createTextNode(
        `${d.day}: ${d.availability === null ? "no data" : Charts.pct(d.availability)}`)));
      if (onPick) r.addEventListener("click", () => onPick(d));
      g.append(r);
    });
    return g;
  },

  // --- a line over time, with a status strip under it -------------------------

  // `points` are /api/history's: [{at, status, ms, num}], oldest first.
  // `key` picks the line ("ms" or "num"). The strip under it is each slot's
  // worst status, so an outage shows as red even where there is no line
  // (a down app has no answer time). Gaps in the data stay gaps.
  line(points, key, { height = 64, unit = "", objective = null, from = null, to = null } = {}) {
    const W = 300, H = height, STRIP = 5, TOP = 4, BOTTOM = H - STRIP - 4;
    const times = points.map((p) => Monitor.parseServerDate(p.at).getTime());
    const t0 = from || (times.length ? times[0] : Date.now() - 86400000);
    const t1 = to || (times.length ? times[times.length - 1] : Date.now());
    const span = Math.max(1, t1 - t0);
    const vals = points.map((p) => p[key]).filter((v) => v !== null && v !== undefined);
    // Scaled to the data, with headroom, so a healthy 40 ms app shows its
    // shape instead of a flat line under a 1 s objective. The objective is
    // drawn only when it is near enough to the data to mean something.
    let max = Math.max(1, ...vals) * 1.15;
    if (objective && objective <= max * 1.5) max = Math.max(max, objective * 1.1);
    else objective = null;
    if (key === "num") max = 100;
    const x = (t) => ((t - t0) / span) * W;
    const y = (v) => BOTTOM - (v / max) * (BOTTOM - TOP);
    const g = Charts.svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", class: "chart", role: "img",
      "aria-label": vals.length ? `Last ${points.length} readings, highest ${Math.round(Math.max(...vals))}${unit}` : "No readings yet" });
    if (objective) {
      g.append(Charts.svg("line", { x1: 0, x2: W, y1: y(objective), y2: y(objective), class: "chart-objective" }));
    }
    // The line, broken wherever a slot has no value.
    let d = "", area = "", run = [];
    const flush = () => {
      if (run.length) {
        d += "M" + run.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join("L");
        area += `M${run[0][0].toFixed(1)},${BOTTOM}L` + run.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join("L")
          + `L${run[run.length - 1][0].toFixed(1)},${BOTTOM}Z`;
      }
      run = [];
    };
    points.forEach((p, i) => {
      const v = p[key];
      if (v === null || v === undefined) { flush(); return; }
      run.push([x(times[i]), y(v)]);
    });
    flush();
    if (area) g.append(Charts.svg("path", { d: area, class: "chart-area" }));
    if (d) g.append(Charts.svg("path", { d, class: "chart-line", "vector-effect": "non-scaling-stroke" }));
    // Status strip.
    const slotW = points.length > 1 ? Math.max(1, (x(times[1]) - x(times[0]))) : W;
    points.forEach((p, i) => {
      g.append(Charts.svg("rect", { x: x(times[i]).toFixed(1), y: H - STRIP, width: (slotW + 0.3).toFixed(1), height: STRIP,
        class: `strip strip-${p.status || "unknown"}` }));
    });
    return g;
  },

  // --- a horizontal meter ------------------------------------------------------

  // `ratio` 0-1 filled; negative draws an empty bar marked over (an error
  // budget already spent).
  meter(ratio, cls) {
    const r = ratio === null || ratio === undefined ? 0 : Math.max(0, Math.min(1, ratio));
    return el("span", { class: "meter" }, el("span", { class: `meter-fill ${cls || ""}`, style: `width:${(r * 100).toFixed(1)}%` }));
  },
};
