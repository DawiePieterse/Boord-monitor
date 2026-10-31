// Two jobs. The push handler is the one that matters: it is what turns a
// message from the farm PC (or the remote watcher) into a notification on a
// locked phone. The app-shell cache is secondary, but it means opening the
// dashboard with no route to the farm still shows a screen that says so,
// rather than Safari's "cannot connect" page.
//
// Bump CACHE together with VERSION in api.js on every frontend change, so
// installed phones fetch the new files.
const CACHE = "boord-monitor-v13";
const SHELL = ["./", "./index.html", "./app.js", "./api.js", "./charts.js", "./styles.css", "./manifest.json",
  "./icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k.startsWith("boord-monitor-") && k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

// Network first, cache as the fallback: a monitor showing a stale build is
// worse than one that takes a second longer to open. API calls are never
// cached - an old status passed off as current would be the worst thing
// this app could do.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  const key = event.request.mode === "navigate" ? new URL("./", self.location.href).href : event.request;
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          event.waitUntil(caches.open(CACHE).then((c) => c.put(key, copy)));
        }
        return res;
      })
      .catch(() => caches.open(CACHE).then((c) => c.match(key)).then((hit) => hit || Response.error())));
});

self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch (e) { msg = { body: event.data && event.data.text() }; }
  // iOS revokes push for a site that receives a push and shows nothing, so
  // every push must end in a notification, even a malformed one.
  event.waitUntil(Promise.all([
    self.registration.showNotification(msg.title || "Boord Monitor", {
      body: msg.body || "",
      tag: msg.tag || "monitor",
      renotify: true,
      icon: "icons/icon-192.png",
      data: { url: msg.url || "/" },
    }),
    setBadge(msg.badge),
  ]));
});

// The number on the Home Screen icon: what is still broken, so a glance at
// the phone says whether the farm is all right without opening anything. A
// push without a count (the test push) leaves it as it is. Never allowed to
// fail the push: the notification above is the part that must happen.
function setBadge(n) {
  const nav = self.navigator;
  if (typeof n !== "number" || !nav || !nav.setAppBadge) return Promise.resolve();
  return (n > 0 ? nav.setAppBadge(n) : nav.clearAppBadge()).catch(() => {});
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.href).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (w.url.startsWith(target) && "focus" in w) return w.focus();
    }
    return self.clients.openWindow(target);
  }));
});
