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

const APP_PATHS = new Set(["/", "/deals", "/tasks", "/contacts", "/activity", "/settings"]);
const ENTITY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

function applicationRootUrl() {
  return new URL("/", self.location.origin).href;
}

function internalNotificationUrl(candidate) {
  const rawCandidate = typeof candidate === "string" ? candidate : "/";
  try {
    const decodedPath = decodeURIComponent(rawCandidate.split(/[?#]/, 1)[0]);
    if (decodedPath.split("/").some((segment) => segment === "." || segment === "..")) {
      return applicationRootUrl();
    }
  } catch {
    return applicationRootUrl();
  }
  let parsed;
  try {
    parsed = new URL(rawCandidate, self.location.origin);
  } catch {
    return applicationRootUrl();
  }
  if (parsed.origin !== self.location.origin || parsed.username || parsed.password) return applicationRootUrl();

  const dynamicMatch = parsed.pathname.match(/^\/(deals|tasks)\/([^/]+)\/?$/);
  if (dynamicMatch) {
    let id;
    try {
      id = decodeURIComponent(dynamicMatch[2]);
    } catch {
      return applicationRootUrl();
    }
    if (!ENTITY_ID_PATTERN.test(id)) return applicationRootUrl();
    const pathname = dynamicMatch[1] === "deals" ? "/deals" : "/tasks";
    const key = pathname === "/deals" ? "deal" : "task";
    return new URL(`${pathname}?${key}=${encodeURIComponent(id)}`, self.location.origin).href;
  }

  const pathname = parsed.pathname.length > 1 ? parsed.pathname.replace(/\/$/, "") : parsed.pathname;
  if (!APP_PATHS.has(pathname)) return applicationRootUrl();
  if (pathname === "/deals" || pathname === "/tasks") {
    const key = pathname === "/deals" ? "deal" : "task";
    const id = parsed.searchParams.get(key);
    if (id && ENTITY_ID_PATTERN.test(id)) {
      return new URL(`${pathname}?${key}=${encodeURIComponent(id)}`, self.location.origin).href;
    }
  }
  return new URL(pathname, self.location.origin).href;
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const targetUrl = internalNotificationUrl(event.notification.data?.url);

    const windowClients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const applicationClients = windowClients.filter((client) => {
      try {
        return new URL(client.url).origin === self.location.origin;
      } catch {
        return false;
      }
    });
    const exactClient = applicationClients.find((client) => client.url === targetUrl);
    if (exactClient) {
      try {
        return await exactClient.focus();
      } catch {
        // A stale window must not prevent opening the requested application route.
      }
    }

    for (const client of applicationClients) {
      if (!("navigate" in client)) continue;
      try {
        const navigated = await client.navigate(targetUrl);
        return await (navigated ?? client).focus();
      } catch {
        // Try another window, then fall back to opening a new application window.
      }
    }
    return self.clients.openWindow(targetUrl);
  })());
});
