// Small helpers shared by the dashboard. Not vendored from Boord: this app
// has one screen and no offline queue, so it needs none of Boord's api.js.
const Monitor = {
  // Boord Monitor's release. Bump on every deploy that touches frontend code,
  // together with CACHE in service-worker.js; scripts/release.sh refuses a
  // tag that disagrees with it, and /api/version reads it back.
  VERSION: "1.3.0",

  async api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    if (opts.body && typeof opts.body !== "string") {
      opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty or not JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.detail) || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  },

  // The server's timestamps are naive UTC (no "Z"), like every sibling app's.
  // Read bare, a browser would take them as local time and every "checked
  // at" on a phone in South Africa would be two hours out.
  parseServerDate(s) {
    if (!s) return null;
    return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
  },

  time(s) {
    const d = Monitor.parseServerDate(s);
    if (!d) return "";
    const today = new Date().toDateString() === d.toDateString();
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return today ? hm : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + hm;
  },

  ago(s) {
    const d = Monitor.parseServerDate(s);
    if (!d) return "";
    const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (sec < 60) return "just now";
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
    return `${Math.round(sec / 86400)} days ago`;
  },
};
