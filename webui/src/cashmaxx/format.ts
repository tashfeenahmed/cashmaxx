// Display and validation helpers. Amounts stay decimal strings on the wire; numbers are used
// only for display and soft comparisons (budgets are small, well inside double precision).

const DECIMAL_RE = /^\d+(\.\d+)?$/;
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const HOST_RE = /^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*(?::\d{1,5})?$/;

export function isNonNegativeDecimal(value: string): boolean {
  return DECIMAL_RE.test(value.trim());
}

export function isAddress(value: string): boolean {
  return ADDRESS_RE.test(value.trim());
}

/** Allowlist entries are wallet addresses or hosts (x402 sellers). */
export function isAllowlistEntry(value: string): boolean {
  const trimmed = value.trim();
  if (trimmed.toLowerCase().startsWith("0x")) return isAddress(trimmed);
  return HOST_RE.test(trimmed);
}

export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

export function toNumber(value: string | number | null | undefined): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function sameDecimal(a: string, b: string): boolean {
  if (isNonNegativeDecimal(a) && isNonNegativeDecimal(b)) return Number(a) === Number(b);
  return a.trim() === b.trim();
}

export type AmountSign = "positive" | "negative" | "zero";

export function amountSign(value: string | number): AmountSign {
  const n = toNumber(value);
  if (n > 0) return "positive";
  if (n < 0) return "negative";
  return "zero";
}

export function formatUsd(value: string | number, locale?: string): string {
  const n = Math.abs(toNumber(value));
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: n !== 0 && n < 0.01 ? 6 : 2,
  }).format(n);
}

/** "+$1.50" / "−$1.50" / "$0.00": the sign is always in the text, not only in the colour. */
export function formatSignedUsd(value: string | number, locale?: string): string {
  const sign = amountSign(value);
  const body = formatUsd(value, locale);
  if (sign === "positive") return `+${body}`;
  if (sign === "negative") return `−${body}`;
  return body;
}

export function shortenAddress(value: string, head = 6, tail = 4): string {
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function formatRelativeTime(iso: string | null, locale?: string, now: number = Date.now()): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return "";
  const seconds = Math.round((at - now) / 1000);
  const abs = Math.abs(seconds);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (abs < 60) return rtf.format(seconds, "second");
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  return rtf.format(Math.round(seconds / 86400), "day");
}

export function formatDateTime(iso: string | null, locale?: string): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return iso;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(at);
}
