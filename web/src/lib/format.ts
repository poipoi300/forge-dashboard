// Number formatting shared by every renderer.

const UNITS = ["B", "KB", "MB", "GB", "TB"];

export function bytes(value: number): string {
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = size >= 100 || unit === 0 ? 0 : 1;
  return `${size.toFixed(digits)} ${UNITS[unit]}`;
}

export function percent(value: number, digits = 0): string {
  return `${value.toFixed(digits)}%`;
}

const compactFormatter = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const fullFormatter = new Intl.NumberFormat();

/** Full under 10,000, compact above: 1,284 / 12.9K / 4.2M. */
export function count(value: number): string {
  return Math.abs(value) < 10_000 ? fullFormatter.format(value) : compactFormatter.format(value);
}

/** Last path segment, for compact path display. */
export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** Shorten the home-directory prefix the way a shell would. */
export function homeRelative(path: string): string {
  const normalized = path.replaceAll("\\", "/");
  const match = /^([A-Za-z]:)?\/Users\/[^/]+/.exec(normalized) ?? /^\/home\/[^/]+/.exec(normalized);
  return match ? `~${normalized.slice(match[0].length)}` : normalized;
}

/** "1 file", "3 files": the count and the noun in agreement. */
export function plural(value: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count(value)} ${value === 1 ? singular : pluralForm}`;
}
