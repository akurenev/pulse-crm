const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function parseAmoUserMapping(raw: string): Record<string, string> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.trim() || "{}");
  } catch {
    throw new Error("Сопоставление пользователей содержит некорректный JSON.");
  }

  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Сопоставление пользователей должно быть JSON-объектом.");
  }

  const mapping: Record<string, string> = {};
  for (const [externalId, internalId] of Object.entries(parsed)) {
    const normalizedExternalId = externalId.trim();
    if (!normalizedExternalId) {
      throw new Error("ID пользователя amoCRM не может быть пустым.");
    }
    if (typeof internalId !== "string" || !UUID_PATTERN.test(internalId.trim())) {
      throw new Error(`Для пользователя amoCRM «${normalizedExternalId}» укажите UUID пользователя Pulse CRM.`);
    }
    mapping[normalizedExternalId] = internalId.trim().toLowerCase();
  }
  return mapping;
}

export function parseSelectOptions(raw: string): string[] {
  const options = raw
    .split(/[\n,]/)
    .map((option) => option.trim())
    .filter(Boolean);
  return [...new Set(options)];
}
