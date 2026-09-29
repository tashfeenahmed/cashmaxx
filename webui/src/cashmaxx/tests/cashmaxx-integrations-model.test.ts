import { describe, expect, it } from "vitest";

import { normalizeIntegrationItem, normalizeIntegrationSummary, normalizeSettings } from "../api";
import {
  buildFieldsPayload,
  daysSinceConnected,
  gmailWarmupCap,
  groupIntegrations,
  initialFormValues,
  integrationStatus,
  parseCap,
  validateIntegrationForm,
  warmupProgress,
} from "../integrations-model";
import type { IntegrationField } from "../types";
import { settingsBody } from "./test-utils";

function field(overrides: Partial<IntegrationField> & { name: string }): IntegrationField {
  return { label: overrides.name, secret: false, required: true, placeholder: "", choices: [], set: false, ...overrides };
}

describe("gmail warm-up", () => {
  it("uses the same ramp as gmail_warmup_cap in the catalog", () => {
    expect([0, 6, 7, 13, 14, 20, 21, 27, 28, 90].map(gmailWarmupCap)).toEqual([10, 10, 20, 20, 40, 40, 80, 80, 400, 400]);
  });

  it("counts whole UTC days from the connection", () => {
    const now = Date.parse("2026-09-26T00:30:00Z");
    expect(daysSinceConnected("2026-09-25T23:59:00Z", now)).toBe(1);
    expect(daysSinceConnected("2026-09-26T00:01:00Z", now)).toBe(0);
    expect(daysSinceConnected("2026-09-17T12:00:00Z", now)).toBe(9);
    expect(daysSinceConnected("not a date", now)).toBeNull();
  });

  it("reports the current week, today's cap and the next step", () => {
    const now = Date.parse("2026-09-26T10:00:00Z");
    expect(warmupProgress({ connectedAt: "2026-09-17T08:00:00Z", warmup: true, dailyCap: 100, now })).toEqual({
      state: "ramping", day: 9, week: 2, cap: 20, nextCap: 40, nextAt: "2026-10-01T00:00:00.000Z",
    });
    expect(warmupProgress({ connectedAt: "2026-09-26T08:00:00Z", warmup: true, dailyCap: 100, now })).toMatchObject({
      state: "ramping", day: 0, week: 1, cap: 10, nextCap: 20,
    });
    expect(warmupProgress({ connectedAt: "2026-09-02T08:00:00Z", warmup: true, dailyCap: 100, now })).toMatchObject({
      state: "ramping", day: 24, week: 4, cap: 80, nextCap: 100,
    });
  });

  it("never exceeds the owner's daily cap", () => {
    const now = Date.parse("2026-09-26T10:00:00Z");
    expect(warmupProgress({ connectedAt: "2026-09-10T08:00:00Z", warmup: true, dailyCap: 25, now })).toMatchObject({
      state: "ramping", week: 3, cap: 25, nextCap: 25,
    });
  });

  it("is done after four weeks, off when disabled, and waits for a connection", () => {
    const now = Date.parse("2026-09-26T10:00:00Z");
    expect(warmupProgress({ connectedAt: "2026-08-29T08:00:00Z", warmup: true, dailyCap: 60, now })).toEqual({
      state: "done", day: 28, cap: 60,
    });
    expect(warmupProgress({ connectedAt: "2026-09-25T08:00:00Z", warmup: false, dailyCap: 60, now })).toEqual({ state: "off", cap: 60 });
    expect(warmupProgress({ connectedAt: null, warmup: true, dailyCap: 60, now })).toEqual({ state: "notStarted" });
  });
});

describe("integration form model", () => {
  const fields = [
    field({ name: "address", value: "agent@example.com", set: true }),
    field({ name: "appPassword", secret: true, set: true }),
    field({ name: "inboxId", required: false }),
    field({ name: "provider", choices: ["cloudflare_quick", "cloudflare_token"] }),
  ];

  it("prefills non-secret values and the first choice, never secrets", () => {
    expect(initialFormValues(fields)).toEqual({
      address: "agent@example.com", appPassword: "", inboxId: "", provider: "cloudflare_quick",
    });
  });

  it("requires required fields, except a stored secret", () => {
    expect(validateIntegrationForm(fields, { address: " ", appPassword: "", inboxId: "", provider: "x" })).toEqual({
      address: "cashmaxx.integrations.dialog.requiredError",
    });
    const unsetSecret = [field({ name: "apiKey", secret: true })];
    expect(validateIntegrationForm(unsetSecret, { apiKey: "" })).toEqual({ apiKey: "cashmaxx.integrations.dialog.requiredError" });
  });

  it("leaves an empty secret out of the payload so the guard keeps it", () => {
    const payload = buildFieldsPayload(fields, { address: " new@example.com ", appPassword: "", inboxId: "", provider: "cloudflare_token" });
    expect(payload).toEqual({ address: "new@example.com", inboxId: "", provider: "cloudflare_token" });
    expect("appPassword" in payload).toBe(false);
    expect(buildFieldsPayload(fields, { address: "a", appPassword: "secret", inboxId: "", provider: "p" }).appPassword).toBe("secret");
  });

  it("parses caps as whole numbers in range", () => {
    expect(parseCap("20", 0, 2000)).toBe(20);
    expect(parseCap("2001", 0, 2000)).toBeNull();
    expect(parseCap("1.5", 0, 2000)).toBeNull();
    expect(parseCap("", 0, 2000)).toBeNull();
  });
});

describe("integration listing model", () => {
  it("derives the status badge", () => {
    const base = { id: "x", label: "X", kind: "guard", category: "social" };
    expect(integrationStatus({ ...base, connected: true, last_test: null })).toBe("connected");
    expect(integrationStatus({ ...base, connected: true, last_test: { ok: false, message: "401", at: null } })).toBe("testFailed");
    expect(integrationStatus({ ...base, connected: false, last_test: { ok: false, message: "", at: null } })).toBe("notConnected");
    expect(integrationStatus({ ...base, connected: null })).toBe("notConnected");
  });

  it("groups in catalog order with unknown categories last", () => {
    const items = ["search", "money", "email", "mystery", "money"].map((category, index) => ({
      id: `i${index}`, label: "", kind: "guard", category, connected: false,
    }));
    expect(groupIntegrations(items).map((group) => [group.category, group.items.length])).toEqual([
      ["money", 2], ["email", 1], ["search", 1], ["other", 1],
    ]);
  });

  it("normalizes listing entries and drops any secret value", () => {
    expect(normalizeIntegrationSummary({ id: "browser", label: "Browser", kind: "agent", category: "browser", connected: null }))
      .toEqual({ id: "browser", label: "Browser", kind: "agent", category: "browser", connected: null });
    const item = normalizeIntegrationItem({
      id: "gmail", label: "Gmail", kind: "guard", category: "email", connected: true,
      fields: [
        { name: "address", label: "Gmail address", secret: false, required: true, placeholder: "", choices: [], set: true, value: "a@b.c" },
        { name: "appPassword", label: "App password", secret: true, required: true, set: true, value: "leaked" },
      ],
      connected_at: "2026-09-01T00:00:00Z", last_test: { ok: true, message: "ok", at: "2026-09-02T00:00:00Z" },
    });
    expect(item?.fields[0].value).toBe("a@b.c");
    expect(item?.fields[1]).not.toHaveProperty("value");
    expect(item?.last_test).toEqual({ ok: true, message: "ok", at: "2026-09-02T00:00:00Z" });
  });

  it("reads the new email, social and hosting settings", () => {
    const settings = normalizeSettings(settingsBody({ emailProvider: "gmail", emailWarmup: false, hostingEnabled: true }));
    expect(settings).toMatchObject({ emailProvider: "gmail", emailDailyCap: 50, emailWarmup: false, socialDailyCap: 3, hostingEnabled: true });
    expect(normalizeSettings(settingsBody({ emailProvider: "carrier-pigeon" })).emailProvider).toBe("none");
  });
});
