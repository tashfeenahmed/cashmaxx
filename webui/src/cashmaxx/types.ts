// Wire shapes for the /api/cashmaxx/* proxy (one-to-one with the guard HTTP API).
// Amounts are decimal strings ("1.50"); times are ISO-8601 UTC.

export type CashmaxxNetwork = "base-sepolia" | "base" | "fake";
export type PnlWindow = "7d" | "30d" | "all";
export type EarningMethod = "digital_products" | "x402_apis" | "bounties" | "agent_marketplaces";
export type SafetyRule = "no_trading" | "no_spam" | "bounty_review" | "loss_stop";
export type ComputePaymentMode = "virtual" | "reimburse" | "owner_topup" | "x402_gateway";

export const ALL_EARNING_METHODS: EarningMethod[] = [
  "digital_products",
  "x402_apis",
  "bounties",
  "agent_marketplaces",
];
export const ALL_RULES: SafetyRule[] = ["no_trading", "no_spam", "bounty_review", "loss_stop"];
export const COMPUTE_MODES: ComputePaymentMode[] = ["virtual", "reimburse", "owner_topup", "x402_gateway"];

export interface CashmaxxHealth {
  ok: boolean;
  network: CashmaxxNetwork | string;
  frozen: boolean;
  version?: string;
}

export interface CashmaxxWallet {
  address: string;
  network: CashmaxxNetwork | string;
  balance_usdc: string;
  available_budget_usd: string;
}

export interface LedgerCategoryRow {
  category: string;
  amount: string;
  direction: "income" | "cost";
}

export interface CashmaxxLedger {
  income: string;
  costs: string;
  net: string;
  by_category: LedgerCategoryRow[];
}

export interface CashmaxxApproval {
  id: string;
  amount_usd: string;
  to: string;
  purpose: string;
  reason: string;
  ts: string | null;
  category?: string;
}

export interface CashmaxxEvent {
  id: string;
  ts: string | null;
  actor: string;
  type: string;
  summary: string;
}

/** `CashmaxxSettings` as the guard serializes it (camelCase aliases). */
export interface CashmaxxSettings {
  network: CashmaxxNetwork | string;
  budgetUsd: string;
  perTxApprovalUsd: string;
  dailyCapUsd: string;
  newRecipientNeedsApproval: boolean;
  maxPaymentsPerHour: number;
  allowlist: string[];
  earningMethods: EarningMethod[];
  rules: SafetyRule[];
  lossStopUsd: string;
  computePaymentMode: ComputePaymentMode;
  ownerWallet: string | null;
  reimburseIntervalHours: number;
  ownerTopupThresholdUsd: string;
  x402GatewayUrl: string | null;
  publicPnl: boolean;
  frozen: boolean;
  frozenReason: string | null;
  /** Optional extra the proxy may add so the UI can show the public P&L link. */
  publicPnlUrl?: string | null;
}

export type CashmaxxSettingsPatch = Partial<
  Omit<CashmaxxSettings, "frozen" | "frozenReason" | "network" | "publicPnlUrl">
>;

export interface OwnerSession {
  session: string;
  expires_at: string;
}
