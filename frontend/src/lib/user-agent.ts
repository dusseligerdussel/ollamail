export interface DeviceInfo {
  browser?: string;
  os?: string;
  mobile: boolean;
}

// Order matters: Edge and Opera also contain "Chrome", Chrome also contains "Safari".
const browsers: [RegExp, string][] = [
  [/Edg(e|A|iOS)?\//, "Edge"],
  [/OPR\/|Opera/, "Opera"],
  [/Firefox\/|FxiOS\//, "Firefox"],
  [/Chrome\/|CriOS\//, "Chrome"],
  [/Safari\//, "Safari"],
];

const systems: [RegExp, string][] = [
  [/Windows/, "Windows"],
  [/iPhone|iPad|iPod/, "iOS"],
  [/Android/, "Android"],
  [/Mac OS X|Macintosh/, "macOS"],
  [/CrOS/, "ChromeOS"],
  [/Linux/, "Linux"],
];

/** Rough browser and OS from a user agent, only to label sessions ("Firefox on Linux"). */
export function parseUserAgent(userAgent: string | null | undefined): DeviceInfo {
  if (!userAgent) return { mobile: false };
  const browser = browsers.find(([pattern]) => pattern.test(userAgent))?.[1];
  const os = systems.find(([pattern]) => pattern.test(userAgent))?.[1];
  return { browser, os, mobile: /Mobi|iPhone|Android/.test(userAgent) };
}
