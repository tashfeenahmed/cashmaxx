// Pure form model for the Cashmaxx settings page: form <-> settings, the PATCH diff and
// client-side validation. Kept free of React so it is easy to test.

import { isAddress, isAllowlistEntry, isHttpUrl, isNonNegativeDecimal, sameDecimal, toNumber } from "./format";
import type {
  CashmaxxSettings,
  CashmaxxSettingsPatch,
  ComputePaymentMode,
  EarningMethod,
  SafetyRule,
} from "./types";

export interface CashmaxxSettingsForm {
  budgetUsd: string;
  perTxApprovalUsd: string;
  dailyCapUsd: string;
  maxPaymentsPerHour: string;
  newRecipientNeedsApproval: boolean;
  allowlist: string[];
  rules: SafetyRule[];
  lossStopUsd: string;
  earningMethods: EarningMethod[];
  computePaymentMode: ComputePaymentMode;
  ownerWallet: string;
  reimburseIntervalHours: string;
  ownerTopupThresholdUsd: string;
  x402GatewayUrl: string;
  publicPnl: boolean;
}

export type SettingsFieldKey = keyof CashmaxxSettingsForm;

export function formFromSettings(settings: CashmaxxSettings): CashmaxxSettingsForm {
  return {
    budgetUsd: settings.budgetUsd,
    perTxApprovalUsd: settings.perTxApprovalUsd,
    dailyCapUsd: settings.dailyCapUsd,
    maxPaymentsPerHour: String(settings.maxPaymentsPerHour),
    newRecipientNeedsApproval: settings.newRecipientNeedsApproval,
    allowlist: [...settings.allowlist],
    rules: [...settings.rules],
    lossStopUsd: settings.lossStopUsd,
    earningMethods: [...settings.earningMethods],
    computePaymentMode: settings.computePaymentMode,
    ownerWallet: settings.ownerWallet ?? "",
    reimburseIntervalHours: String(settings.reimburseIntervalHours),
    ownerTopupThresholdUsd: settings.ownerTopupThresholdUsd,
    x402GatewayUrl: settings.x402GatewayUrl ?? "",
    publicPnl: settings.publicPnl,
  };
}

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  const left = [...new Set(a)].sort();
  const right = [...new Set(b)].sort();
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/** Only the fields the owner changed, in the guard's camelCase wire names. */
export function diffSettings(original: CashmaxxSettings, form: CashmaxxSettingsForm): CashmaxxSettingsPatch {
  const patch: CashmaxxSettingsPatch = {};
  const base = formFromSettings(original);
  const decimals = ["budgetUsd", "perTxApprovalUsd", "dailyCapUsd", "lossStopUsd", "ownerTopupThresholdUsd"] as const;
  for (const key of decimals) {
    if (!sameDecimal(base[key], form[key])) patch[key] = form[key].trim();
  }
  const ints = ["maxPaymentsPerHour", "reimburseIntervalHours"] as const;
  for (const key of ints) {
    if (Number(base[key]) !== Number(form[key])) patch[key] = Math.trunc(Number(form[key]));
  }
  if (base.newRecipientNeedsApproval !== form.newRecipientNeedsApproval) {
    patch.newRecipientNeedsApproval = form.newRecipientNeedsApproval;
  }
  if (base.publicPnl !== form.publicPnl) patch.publicPnl = form.publicPnl;
  if (base.computePaymentMode !== form.computePaymentMode) patch.computePaymentMode = form.computePaymentMode;
  const allowlist = form.allowlist.map((entry) => entry.trim()).filter(Boolean);
  if (!sameList(base.allowlist, allowlist)) patch.allowlist = allowlist;
  if (!sameSet(base.rules, form.rules)) patch.rules = [...form.rules].sort();
  if (!sameSet(base.earningMethods, form.earningMethods)) patch.earningMethods = [...form.earningMethods].sort();
  if (base.ownerWallet.trim() !== form.ownerWallet.trim()) patch.ownerWallet = form.ownerWallet.trim() || null;
  if (base.x402GatewayUrl.trim() !== form.x402GatewayUrl.trim()) {
    patch.x402GatewayUrl = form.x402GatewayUrl.trim() || null;
  }
  return patch;
}

export interface SettingsValidation {
  /** field -> i18n key of the error */
  errors: Partial<Record<SettingsFieldKey, string>>;
  /** i18n keys of soft warnings (they do not block saving) */
  warnings: string[];
}

function intInRange(value: string, min: number, max: number): boolean {
  if (!/^\d+$/.test(value.trim())) return false;
  const n = Number(value);
  return n >= min && n <= max;
}

export function validateSettingsForm(form: CashmaxxSettingsForm): SettingsValidation {
  const errors: SettingsValidation["errors"] = {};
  const decimals = ["budgetUsd", "perTxApprovalUsd", "dailyCapUsd", "lossStopUsd", "ownerTopupThresholdUsd"] as const;
  for (const key of decimals) {
    if (!isNonNegativeDecimal(form[key])) errors[key] = "cashmaxx.validation.decimal";
  }
  if (!intInRange(form.maxPaymentsPerHour, 1, 1000)) errors.maxPaymentsPerHour = "cashmaxx.validation.maxPerHour";
  if (!intInRange(form.reimburseIntervalHours, 1, 720)) {
    errors.reimburseIntervalHours = "cashmaxx.validation.interval";
  }
  if (form.allowlist.some((entry) => !isAllowlistEntry(entry))) errors.allowlist = "cashmaxx.validation.allowlist";
  const wallet = form.ownerWallet.trim();
  if (wallet && !isAddress(wallet)) errors.ownerWallet = "cashmaxx.validation.address";
  else if (!wallet && form.computePaymentMode === "reimburse") errors.ownerWallet = "cashmaxx.validation.ownerWalletRequired";
  const gateway = form.x402GatewayUrl.trim();
  if (gateway && !isHttpUrl(gateway)) errors.x402GatewayUrl = "cashmaxx.validation.url";
  else if (!gateway && form.computePaymentMode === "x402_gateway") {
    errors.x402GatewayUrl = "cashmaxx.validation.gatewayRequired";
  }

  const warnings: string[] = [];
  if (!errors.perTxApprovalUsd && !errors.dailyCapUsd && toNumber(form.perTxApprovalUsd) > toNumber(form.dailyCapUsd)) {
    warnings.push("cashmaxx.validation.perTxOverDaily");
  }
  if (!errors.dailyCapUsd && !errors.budgetUsd && toNumber(form.dailyCapUsd) > toNumber(form.budgetUsd)) {
    warnings.push("cashmaxx.validation.dailyOverBudget");
  }
  return { errors, warnings };
}
