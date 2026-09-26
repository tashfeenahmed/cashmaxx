import { describe, expect, it } from "vitest";

import { normalizeApproval, normalizeLedger, normalizeSettings } from "../api";
import { formatSignedUsd, isAddress, isAllowlistEntry, isNonNegativeDecimal } from "../format";
import { diffSettings, formFromSettings, validateSettingsForm } from "../settings-form";
import { settingsBody } from "./test-utils";

describe("cashmaxx settings model", () => {
  const original = normalizeSettings(settingsBody());

  it("produces an empty diff for an untouched form", () => {
    expect(diffSettings(original, formFromSettings(original))).toEqual({});
  });

  it("diffs only changed fields, with wire types", () => {
    const form = {
      ...formFromSettings(original),
      dailyCapUsd: "12.5",
      maxPaymentsPerHour: "30",
      earningMethods: ["bounties" as const],
      computePaymentMode: "reimburse" as const,
      ownerWallet: " 0x4444444444444444444444444444444444444444 ",
    };
    expect(diffSettings(original, form)).toEqual({
      dailyCapUsd: "12.5",
      maxPaymentsPerHour: 30,
      earningMethods: ["bounties"],
      computePaymentMode: "reimburse",
      ownerWallet: "0x4444444444444444444444444444444444444444",
    });
  });

  it("treats set fields as unordered", () => {
    const form = { ...formFromSettings(original), rules: [...original.rules].reverse() };
    expect(diffSettings(original, form)).toEqual({});
  });

  it("validates decimals, addresses and soft cap ordering", () => {
    const form = { ...formFromSettings(original), budgetUsd: "-1", perTxApprovalUsd: "20", ownerWallet: "0x1" };
    const result = validateSettingsForm(form);
    expect(result.errors.budgetUsd).toBe("cashmaxx.validation.decimal");
    expect(result.errors.ownerWallet).toBe("cashmaxx.validation.address");
    expect(result.warnings).toEqual(["cashmaxx.validation.perTxOverDaily"]);
  });

  it("accepts the settings wrapped in {settings}", () => {
    expect(normalizeSettings({ settings: settingsBody({ budgetUsd: "99" }) }).budgetUsd).toBe("99");
  });
});

describe("cashmaxx format and normalizers", () => {
  it("validates inputs", () => {
    expect(isNonNegativeDecimal("0")).toBe(true);
    expect(isNonNegativeDecimal("2.50")).toBe(true);
    expect(isNonNegativeDecimal("-1")).toBe(false);
    expect(isNonNegativeDecimal("1e3")).toBe(false);
    expect(isAddress("0x" + "a".repeat(40))).toBe(true);
    expect(isAddress("0x" + "g".repeat(40))).toBe(false);
    expect(isAllowlistEntry("api.example.com")).toBe(true);
    expect(isAllowlistEntry("0x123")).toBe(false);
  });

  it("keeps the sign in the text", () => {
    expect(formatSignedUsd("1.5", "en")).toBe("+$1.50");
    expect(formatSignedUsd("-2", "en")).toBe("−$2.00");
    expect(formatSignedUsd("0", "en")).toBe("$0.00");
  });

  it("normalizes ledger categories from a map or a list", () => {
    expect(normalizeLedger({ income: "1", costs: "2", net: "-1", by_category: { compute: "2", sale_stripe: "1" } }).by_category)
      .toEqual([
        { category: "compute", amount: "2", direction: "cost" },
        { category: "sale_stripe", amount: "1", direction: "income" },
      ]);
    expect(normalizeLedger({ by_category: [{ category: "other", direction: "income", amount_usd: "3" }] }).by_category)
      .toEqual([{ category: "other", amount: "3", direction: "income" }]);
  });

  it("normalizes approvals with a nested payment", () => {
    expect(normalizeApproval({ id: "x", reason: "new_recipient", payment: { to_addr: "0xa", amount_usd: "1", purpose: "p" } }))
      .toMatchObject({ id: "x", to: "0xa", amount_usd: "1", purpose: "p", reason: "new_recipient" });
  });
});
