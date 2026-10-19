// Small helpers shared by the dashboard. Not vendored from Boord: this app
// has one screen and no offline queue, so it needs none of Boord's api.js.
const Monitor = {
  // Boord Monitor's release. Bump on every deploy that touches frontend code,
  // together with CACHE in service-worker.js; scripts/release.sh refuses a
  // tag that disagrees with it, and /api/version reads it back.
  VERSION: "1.7",

  // A phone whose WiFi or mobile data is up but which cannot actually reach
  // the farm (Tailscale asleep, the PC off) gets no error from fetch() - the
  // request just hangs until the OS gives up, which can be minutes. Every
  // request therefore gets a deadline, like Boord's api.js, and a blown
  // deadline reads as an ordinary "can't reach" so the dashboard keeps the
  // last picture on screen instead of a spinner.
  NETWORK_TIMEOUT_MS: 8000,
  OFFLINE_MESSAGE: "Can't reach the monitor - check Tailscale",

  // True when the request never got an answer (offline, unreachable, timed
  // out) rather than an answer that said no.
  isNetworkError(e) {
    return e instanceof TypeError || (!!e && (e.name === "AbortError" || e.name === "TimeoutError" || e.network === true));
  },

  // The words to show for a failure. Browsers word a dropped connection as
  // "Failed to fetch" (Chrome) or "Load failed" (Safari), which tells someone
  // in the orchard nothing; and FastAPI's 422 detail is a list of objects,
  // which would print as "[object Object]".
  errorText(e) {
    if (Monitor.isNetworkError(e)) return Monitor.OFFLINE_MESSAGE;
    return (e && e.message) || "Something went wrong";
  },

  async api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    if (opts.body && typeof opts.body !== "string") {
      opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
      opts.body = JSON.stringify(opts.body);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Monitor.NETWORK_TIMEOUT_MS);
    let res;
    let data = null;
    try {
      res = await fetch(path, { ...opts, signal: controller.signal });
      try { data = await res.json(); } catch (e) { /* empty or not JSON */ }
    } catch (e) {
      // Thrown as an Error carrying the plain message, so a caller that
      // shows err.message directly still shows words a person can act on.
      const err = new Error(Monitor.OFFLINE_MESSAGE);
      err.network = true;
      throw err;
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      const detail = data && data.detail;
      const text = Array.isArray(detail)
        ? (detail[0] && detail[0].msg) || `HTTP ${res.status}`
        : (typeof detail === "string" && detail) || `HTTP ${res.status}`;
      const err = new Error(text);
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
    const hm = Monitor.hm(d);
    return today ? hm : Monitor.day(d) + " " + hm;
  },

  // en-ZA and 24-hour, like every sibling app: the phones' own locale is
  // often en-US, and "4:10 PM" next to Boord's "16:10" for the same moment
  // makes people wonder which one is wrong.
  hm(d) {
    return d.toLocaleTimeString("en-ZA", { hour: "2-digit", minute: "2-digit", hour12: false });
  },

  day(d) {
    return d.toLocaleDateString("en-ZA", { day: "numeric", month: "short" });
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
