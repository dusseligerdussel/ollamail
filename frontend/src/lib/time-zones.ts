/** The browser's IANA time zone, `UTC` if unknown. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/**
 * IANA time zones offered for selection, sorted, always including `UTC` and `current` (so a
 * stored value the browser does not know stays selectable).
 */
export function timeZoneOptions(current?: string): string[] {
  let zones: string[] = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    // Older engines: only the fallback entries below.
  }
  const all = new Set([...zones, "UTC", browserTimeZone()]);
  if (current) all.add(current);
  return [...all].sort((a, b) => a.localeCompare(b));
}
