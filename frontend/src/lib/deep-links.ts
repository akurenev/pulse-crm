const ENTITY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

const SIMPLE_APP_PATHS = new Set(["/", "/deals", "/tasks", "/contacts", "/activity", "/settings"]);

export type DeepLinkEntity = "deal" | "task" | "contact" | "company";

export function safeEntityId(value: string | null | undefined): string | null {
  if (!value || !ENTITY_ID_PATTERN.test(value)) return null;
  return value;
}

export function entityDeepLink(pathname: "/deals" | "/tasks", entityId: string): string {
  const key = pathname === "/deals" ? "deal" : "task";
  return `${pathname}?${key}=${encodeURIComponent(entityId)}`;
}

/**
 * Restrict notification/login redirects to known application screens. Besides
 * keeping navigation same-origin, this prevents untrusted payloads from using
 * the service worker or router as an open redirect to an API/static path.
 */
export function normalizeInternalAppPath(candidate: unknown, origin?: string): string | null {
  if (typeof candidate !== "string" || !candidate || candidate.startsWith("//")) return null;
  try {
    const decodedPath = decodeURIComponent(candidate.split(/[?#]/, 1)[0]);
    if (decodedPath.split("/").some((segment) => segment === "." || segment === "..")) return null;
  } catch {
    return null;
  }
  const baseOrigin = origin ?? (typeof window === "undefined" ? "https://app.example.test" : window.location.origin);
  let parsed: URL;
  try {
    parsed = new URL(candidate, baseOrigin);
  } catch {
    return null;
  }
  let normalizedOrigin: string;
  try {
    normalizedOrigin = new URL(baseOrigin).origin;
  } catch {
    return null;
  }
  if (parsed.origin !== normalizedOrigin || parsed.username || parsed.password) return null;

  const dynamicMatch = parsed.pathname.match(/^\/(deals|tasks)\/([^/]+)\/?$/);
  if (dynamicMatch) {
    let decodedId: string;
    try {
      decodedId = decodeURIComponent(dynamicMatch[2]);
    } catch {
      return null;
    }
    const id = safeEntityId(decodedId);
    if (!id) return null;
    return entityDeepLink(dynamicMatch[1] === "deals" ? "/deals" : "/tasks", id);
  }

  const pathname = parsed.pathname.length > 1 ? parsed.pathname.replace(/\/$/, "") : parsed.pathname;
  if (!SIMPLE_APP_PATHS.has(pathname)) return null;
  if (pathname === "/deals" || pathname === "/tasks") {
    const key = pathname === "/deals" ? "deal" : "task";
    const id = safeEntityId(parsed.searchParams.get(key));
    return id ? entityDeepLink(pathname, id) : pathname;
  }
  if (pathname === "/contacts") {
    const contactId = safeEntityId(parsed.searchParams.get("contact"));
    if (contactId) return `/contacts?contact=${encodeURIComponent(contactId)}`;
    const companyId = safeEntityId(parsed.searchParams.get("company"));
    return companyId ? `/contacts?company=${encodeURIComponent(companyId)}` : pathname;
  }
  return pathname;
}

export function deepLinkEntityId(searchParams: URLSearchParams, entity: DeepLinkEntity): string | null {
  return safeEntityId(searchParams.get(entity));
}
