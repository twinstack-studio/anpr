/* TwinStack Gate service worker: phone notifications for residents (visitor at the gate). */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { d = { title: "TwinStack Gate", body: e.data ? e.data.text() : "" }; }
  const ask = !!d.request_id;
  e.waitUntil(self.registration.showNotification(d.title || "TwinStack Gate", {
    body: d.body || "",
    tag: d.tag,
    icon: "/society/icon-192.png",
    badge: "/society/icon-192.png",
    data: { url: d.url || "/society/", request_id: d.request_id },
    requireInteraction: ask,
    vibrate: ask ? [200, 100, 200, 100, 200] : [100],
    actions: ask ? [{ action: "approved", title: "✓ Let in" }, { action: "rejected", title: "✗ Refuse" }] : [],
  }));
});

self.addEventListener("notificationclick", (e) => {
  const { url, request_id } = e.notification.data || {};
  e.notification.close();
  if (request_id && (e.action === "approved" || e.action === "rejected")) {
    e.waitUntil(fetch(`/api/soc/requests/${request_id}/answer`, {
      method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer: e.action }),
    }).then(async (r) => {
      const msg = r.ok ? (e.action === "approved" ? "The guard has been told to let them in." : "The guard has been told to refuse.")
        : ((await r.json().catch(() => ({}))).detail || "Could not send your answer. Open the app.");
      return self.registration.showNotification(r.ok ? "Answer sent" : "Not sent", { body: msg, tag: "answer", icon: "/society/icon-192.png" });
    }).catch(() => self.clients.openWindow(url || "/society/")));
    return;
  }
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    const open = list.find((c) => c.url.includes("/society/"));
    if (open) { open.navigate(url || "/society/").catch(() => {}); return open.focus(); }
    return self.clients.openWindow(url || "/society/");
  }));
});
