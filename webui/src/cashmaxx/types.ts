// Wire shapes for the /api/cashmaxx/* proxy (one-to-one with the guard HTTP API).
// Amounts are decimal strings ("1.50"); times are ISO-8601 UTC.

export type CashmaxxNetwork = "base-sepolia" | "base" | "fake";
export type PnlWindow = "7d" | "30d" | "all";
export type EarningMethod = "digital_products" | "x402_apis" | "bounties" | "agent_marketplaces";
export type SafetyRule = "no_trading" | "no_spam" | "bounty_review" | "loss_stop";
export type ComputePaymentMode = "virtual" | "reimburse" | "owner_topup" | "x402_gateway";
export type EmailProvider = "none" | "gmail" | "agentmail";

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
  /** Outbound email: which connected integration sends (`none` = email off). */
  emailProvider: EmailProvider;
  /** Recipients per UTC day across all sends. */
  emailDailyCap: number;
  /** Gmail only: ramp the cap over the first four weeks after connecting. */
  emailWarmup: boolean;
  /** Posts per UTC day across every social platform. */
  socialDailyCap: number;
  /** Lets the agent expose a local port through the hosting integration. */
  hostingEnabled: boolean;
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

// ---- integrations (see docs/cashmaxx/ARCHITECTURE.md, "Integrations") --------------------

export type IntegrationKind = "guard" | "agent";
export type IntegrationCategory =
  | "money"
  | "compute"
  | "approvals"
  | "email"
  | "social"
  | "hosting"
  | "browser"
  | "code"
  | "search";

export const INTEGRATION_CATEGORIES: IntegrationCategory[] = [
  "money",
  "compute",
  "approvals",
  "email",
  "social",
  "hosting",
  "browser",
  "code",
  "search",
];

export interface IntegrationField {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  /** Non-empty: render a select. */
  choices: string[];
  /** Whether a value is stored (secrets are never returned, only this flag). */
  set: boolean;
  /** Current value, non-secret fields only. */
  value?: string | null;
}

export interface IntegrationTestResult {
  ok: boolean;
  message: string;
  at: string | null;
}

/** An entry of the plain `GET /integrations` listing (no owner session). */
export interface IntegrationSummary {
  id: string;
  label: string;
  kind: IntegrationKind | string;
  category: IntegrationCategory | string;
  /** `null` while an agent-kind entry has not been resolved by the proxy. */
  connected: boolean | null;
}

/** The owner listing adds the field specs and the test history. */
export interface IntegrationItem extends IntegrationSummary {
  summary: string;
  docs_url: string;
  fields: IntegrationField[];
  connected_at: string | null;
  last_test: IntegrationTestResult | null;
}
