const MAX_DEAL_TAGS = 100;
const MAX_DEAL_TAG_LENGTH = 100;

export function normalizeDealTags(tags: string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const value of tags) {
    const tag = value.trim().replace(/^#+/, "").trim().slice(0, MAX_DEAL_TAG_LENGTH).trim();
    const key = tag.toLocaleLowerCase("ru");
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    normalized.push(tag);
    if (normalized.length === MAX_DEAL_TAGS) break;
  }
  return normalized;
}

export function parseDealTags(value: string): string[] {
  return normalizeDealTags(value.split(/[\n,]/));
}
