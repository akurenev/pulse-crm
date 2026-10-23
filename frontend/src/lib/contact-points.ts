export type ContactPointKind = "email" | "phone";

export const MAX_CONTACT_POINTS = 20;

export function contactPointValues(primary?: string | null, values: string[] = []): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const value of [primary, ...values]) {
    const trimmed = value?.trim();
    if (!trimmed || trimmed === "—" || seen.has(trimmed)) continue;
    seen.add(trimmed);
    normalized.push(trimmed);
  }

  return normalized;
}

export function readContactPoints(formData: FormData, kind: ContactPointKind): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const value of formData.getAll(kind)) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (!trimmed) continue;
    const key = kind === "email" ? trimmed.toLowerCase() : trimmed;
    if (seen.has(key)) continue;
    seen.add(key);
    normalized.push(trimmed);
  }

  return normalized;
}
