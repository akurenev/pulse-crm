/* Pulse CRM deliberately stays network-only: client and API data are never cached here. */
self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data?.json() ?? {};
  } catch {
    payload = { body: event.data?.text() ?? "" };
  }

  const title = typeof payload.title === "string" && payload.title ? payload.title : "Pulse CRM";
  const body = typeof payload.body === "string" ? payload.body : "Новое уведомление";
  const url = typeof payload.url === "string" ? payload.url : "/";
  const options = {
    body,
    icon: "/icons/pulse-crm-192.png",
    badge: "/icons/pulse-crm-192.png",
    data: { url },
    ...(typeof payload.tag === "string" ? { tag: payload.tag } : {}),
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const candidate = event.notification.data?.url;
    let targetUrl = self.location.origin;
    try {
      const parsed = new URL(typeof candidate === "string" ? candidate : "/", self.location.origin);
      if (parsed.origin === self.location.origin) targetUrl = parsed.href;
    } catch {
      // Invalid or cross-origin targets fall back to the application root.
    }

    const windowClients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windowClients) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      if ("navigate" in client) {
        try {
          await client.navigate(targetUrl);
        } catch {
          // A focused existing window is still preferable if navigation is blocked.
        }
      }
      return client.focus();
    }
    return self.clients.openWindow(targetUrl);
  })());
});
