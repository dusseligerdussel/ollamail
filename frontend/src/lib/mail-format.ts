import type { Address } from "@/api/mail";

// Creating an `Intl.DateTimeFormat` is far more expensive than formatting with it, and list rows
// format a date each (#111): formatters are created once per locale, time zone and style.
const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(locale: string, timeZone: string, options: Intl.DateTimeFormatOptions) {
  const key = `${locale}|${timeZone}|${JSON.stringify(options)}`;
  let cached = formatters.get(key);
  if (!cached) {
    cached = new Intl.DateTimeFormat(locale, { timeZone, ...options });
    formatters.set(key, cached);
  }
  return cached;
}

function sameDay(a: Date, b: Date, timeZone: string) {
  const day = formatter("en-CA", timeZone, { dateStyle: "short" });
  return day.format(a) === day.format(b);
}

function sameYear(a: Date, b: Date, timeZone: string) {
  const year = formatter("en-CA", timeZone, { year: "numeric" });
  return year.format(a) === year.format(b);
}

/** Compact date for list rows: time today, day and month this year, else the full date. */
export function formatListDate(value: string, locale: string, timeZone: string, now = new Date()) {
  const date = new Date(value);
  if (sameDay(date, now, timeZone)) {
    return formatter(locale, timeZone, { timeStyle: "short" }).format(date);
  }
  if (sameYear(date, now, timeZone)) {
    return formatter(locale, timeZone, { day: "numeric", month: "short" }).format(date);
  }
  return formatter(locale, timeZone, { dateStyle: "medium" }).format(date);
}

export function formatDateTime(value: string, locale: string, timeZone: string) {
  return formatter(locale, timeZone, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

const sizeUnits = ["byte", "kilobyte", "megabyte", "gigabyte"] as const;

export function formatSize(bytes: number, locale: string) {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < sizeUnits.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: sizeUnits[unit],
    unitDisplay: "short",
    maximumFractionDigits: unit === 0 ? 0 : 1,
  }).format(value);
}

/** Name if known, else the address. */
export function addressName(address: Address | null | undefined) {
  return address ? address.name || address.address : "";
}

/** `Name <address>` or just the address. */
export function addressFull(address: Address) {
  return address.name ? `${address.name} <${address.address}>` : address.address;
}
