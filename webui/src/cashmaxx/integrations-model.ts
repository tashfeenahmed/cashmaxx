// Pure model for the Integrations tab: grouping, status, the connect form and the Gmail
// warm-up ramp. Kept free of React so it is easy to test.

import {
  INTEGRATION_CATEGORIES,
  type IntegrationCategory,
  type IntegrationField,
  type IntegrationItem,
  type IntegrationSummary,
} from "./types";

export type IntegrationStatus = "connected" | "notConnected" | "testFailed";

/** "Test failed" wins over "connected": a stored credential that no longer works. */
export function integrationStatus(item: IntegrationSummary & { last_test?: IntegrationItem["last_test"] }): IntegrationStatus {
  if (item.connected && item.last_test && !item.last_test.ok) return "testFailed";
  return item.connected ? "connected" : "notConnected";
}

export interface IntegrationGroup<T extends IntegrationSummary> {
  category: IntegrationCategory | "other";
  items: T[];
}

/** Items grouped in the catalog's category order; unknown categories go last as "other". */
export function groupIntegrations<T extends IntegrationSummary>(items: T[]): IntegrationGroup<T>[] {
  const known = new Set<string>(INTEGRATION_CATEGORIES);
  const groups: IntegrationGroup<T>[] = INTEGRATION_CATEGORIES.map((category) => ({
    category,
    items: items.filter((item) => item.category === category),
  }));
  groups.push({ category: "other", items: items.filter((item) => !known.has(item.category)) });
  return groups.filter((group) => group.items.length > 0);
}

// ---- connect / edit form -------------------------------------------------------------------

export type IntegrationFormValues = Record<string, string>;

/** Initial values: non-secret fields show their stored value, secrets always start empty. */
export function initialFormValues(fields: IntegrationField[]): IntegrationFormValues {
  const values: IntegrationFormValues = {};
  for (const field of fields) {
    if (field.secret) values[field.name] = "";
    else if (field.value) values[field.name] = field.value;
    else values[field.name] = field.choices.length ? field.choices[0] : "";
  }
  return values;
}

/** field name -> i18n key of the error */
export function validateIntegrationForm(
  fields: IntegrationField[],
  values: IntegrationFormValues,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    if (!field.required) continue;
    const value = (values[field.name] ?? "").trim();
    // A stored secret may be left empty: the guard keeps it.
    if (!value && !(field.secret && field.set)) errors[field.name] = "cashmaxx.integrations.dialog.requiredError";
  }
  return errors;
}

/**
 * The `fields` payload for `cashmaxx.integrations.update`. Secrets are sent only when the owner
 * typed one, so "leave empty to keep" never sends (or clears) a stored secret. Non-secret values
 * are always sent so an optional one can be cleared.
 */
export function buildFieldsPayload(fields: IntegrationField[], values: IntegrationFormValues): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const field of fields) {
    const value = (values[field.name] ?? "").trim();
    if (field.secret) {
      if (value) payload[field.name] = value;
    } else {
      payload[field.name] = value;
    }
  }
  return payload;
}

// ---- Gmail warm-up -----------------------------------------------------------------------

const DAY_MS = 86_400_000;
export const WARMUP_WEEKS = 4;

/** Same ramp as `gmail_warmup_cap` in cashmaxx/integrations_catalog.py. */
export function gmailWarmupCap(daysSinceConnected: number): number {
  if (daysSinceConnected < 7) return 10;
  if (daysSinceConnected < 14) return 20;
  if (daysSinceConnected < 21) return 40;
  if (daysSinceConnected < 28) return 80;
  return 400;
}

function utcDay(ms: number): number {
  return Math.floor(ms / DAY_MS);
}

/** Whole UTC days since the connection; day 0 is the day Gmail was connected. */
export function daysSinceConnected(connectedAt: string, now: number = Date.now()): number | null {
  const at = Date.parse(connectedAt);
  if (!Number.isFinite(at)) return null;
  return Math.max(0, utcDay(now) - utcDay(at));
}

export type WarmupProgress =
  | { state: "notStarted" }
  | { state: "off"; cap: number }
  | { state: "done"; day: number; cap: number }
  | {
      state: "ramping";
      day: number;
      /** 1..4 */
      week: number;
      /** Today's effective cap: min(daily cap, ramp step). */
      cap: number;
      /** The next step's effective cap and the UTC date it starts. */
      nextCap: number;
      nextAt: string;
    };

export function warmupProgress({
  connectedAt,
  warmup,
  dailyCap,
  now = Date.now(),
}: {
  connectedAt: string | null;
  warmup: boolean;
  dailyCap: number;
  now?: number;
}): WarmupProgress {
  if (!warmup) return { state: "off", cap: dailyCap };
  const day = connectedAt ? daysSinceConnected(connectedAt, now) : null;
  if (day === null || !connectedAt) return { state: "notStarted" };
  if (day >= WARMUP_WEEKS * 7) return { state: "done", day, cap: Math.min(dailyCap, gmailWarmupCap(day)) };
  const week = Math.floor(day / 7) + 1;
  const nextDay = week * 7;
  const startDay = utcDay(Date.parse(connectedAt));
  return {
    state: "ramping",
    day,
    week,
    cap: Math.min(dailyCap, gmailWarmupCap(day)),
    nextCap: Math.min(dailyCap, gmailWarmupCap(nextDay)),
    nextAt: new Date((startDay + nextDay) * DAY_MS).toISOString(),
  };
}

/** Whole number in [min, max], as the settings' caps are validated by the guard. */
export function parseCap(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const n = Number(value);
  return n >= min && n <= max ? n : null;
}
