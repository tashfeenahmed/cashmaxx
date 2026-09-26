import type { WebUIMutationTransport } from "@/lib/api";
import { fetchWithTimeout } from "@/lib/http";

import type {
  CashmaxxApproval,
  CashmaxxEvent,
  CashmaxxHealth,
  CashmaxxLedger,
  CashmaxxSettings,
  CashmaxxSettingsPatch,
  CashmaxxWallet,
  ComputePaymentMode,
  EarningMethod,
  LedgerCategoryRow,
  OwnerSession,
  PnlWindow,
  SafetyRule,
} from "./types";

export const CASHMAXX_API_BASE = "/api/cashmaxx";
const TIMEOUT_MS = 20_000;

/**
 * How the pages reach the gateway. The nanobot WebUI HTTP surface only serves authenticated
 * GETs, so reads go over `fetch` with the WebUI bearer token and every state change goes over
 * the authenticated WebUI socket as a `webui_request` action (see
 * `cashmaxx/plugin/webui_routes.py`). The owner session travels in `payload.owner_session`.
 */
export interface CashmaxxConnection {
  token: string;
  transport: WebUIMutationTransport;
}

/** Error carrying the guard's `{error, message}` plus the HTTP status. */
export class CashmaxxError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message || code || `HTTP ${status}`);
    this.name = "CashmaxxError";
    this.status = status;
    this.code = code;
  }
}

/** The gateway could not reach the guard (or Cashmaxx is not set up on this gateway). */
export function isGuardUnavailable(error: unknown): boolean {
  return error instanceof CashmaxxError && error.status === 503 && error.code !== "transport";
}

export function isNotConfigured(error: unknown): boolean {
  return error instanceof CashmaxxError && error.code === "not_configured";
}

export function needsOwner(error: unknown): boolean {
  return error instanceof CashmaxxError && (error.status === 401 || error.status === 403);
}

function errorFromBody(status: number, body: unknown, fallback: string): CashmaxxError {
  const record = asRecord(body);
  // A bad gateway / gateway timeout means the guard is not answering.
  const normalized = status === 502 || status === 504 ? 503 : status;
  return new CashmaxxError(
    normalized,
    str(record.error) || (normalized === 503 ? "guard_unavailable" : "error"),
    str(record.message) || str(record.error) || fallback,
  );
}

/** Authenticated GET under /api/cashmaxx, the same way `lib/api.ts` reads settings. */
export async function cashmaxxGet<T = unknown>(
  token: string,
  path: string,
  query: Record<string, string | undefined> = {},
): Promise<T> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, value);
  }
  const qs = params.toString();
  let res: Response;
  try {
    res = await fetchWithTimeout(
      `${CASHMAXX_API_BASE}${path}${qs ? `?${qs}` : ""}`,
      { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
      TIMEOUT_MS,
    );
  } catch (error) {
    throw new CashmaxxError(503, "guard_unavailable", error instanceof Error ? error.message : String(error));
  }
  const payload: unknown = await res.json().catch(() => null);
  if (!res.ok) throw errorFromBody(res.status, payload, `HTTP ${res.status}`);
  return payload as T;
}

/** A `cashmaxx.*` WebUI socket action. Errors carry the guard's JSON body as their message. */
export async function cashmaxxMutation<T = unknown>(
  transport: WebUIMutationTransport,
  action: string,
  payload: Record<string, unknown>,
): Promise<T> {
  try {
    return await transport.requestMutation<T>(action, payload, TIMEOUT_MS);
  } catch (reason) {
    const status = typeof reason === "object" && reason !== null && "status" in reason
      && typeof reason.status === "number" ? reason.status : 500;
    const message = reason instanceof Error ? reason.message : String(reason ?? "");
    let body: unknown = null;
    if (message.trim().startsWith("{")) {
      try {
        body = JSON.parse(message);
      } catch {
        body = null;
      }
    }
    if (body === null) {
      // Not from the guard proxy: the WebUI socket itself failed (closed, timed out...).
      throw new CashmaxxError(status, status === 503 || status === 504 ? "transport" : "error", message);
    }
    throw errorFromBody(status, body, message);
  }
}

// ---- normalizers (tolerant of small shape differences in the proxy) ----------------------

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function str(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function listFrom(value: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(value)) return value;
  const record = asRecord(value);
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

const INCOME_CATEGORIES = new Set(["sale_stripe", "sale_x402", "transfer_in", "bounty", "marketplace"]);

export function categoryDirection(category: string): "income" | "cost" {
  return INCOME_CATEGORIES.has(category) ? "income" : "cost";
}

function normalizeCategories(value: unknown): LedgerCategoryRow[] {
  const rows: LedgerCategoryRow[] = [];
  const push = (category: string, amount: string, direction?: unknown) => {
    if (!category) return;
    rows.push({
      category,
      amount: amount || "0",
      direction: direction === "income" || direction === "cost" ? direction : categoryDirection(category),
    });
  };
  if (Array.isArray(value)) {
    for (const item of value) {
      const row = asRecord(item);
      push(str(row.category), str(row.amount_usd) || str(row.amount) || str(row.total) || str(row.net), row.direction);
    }
    return rows;
  }
  for (const [category, entry] of Object.entries(asRecord(value))) {
    if (typeof entry === "string" || typeof entry === "number") {
      push(category, str(entry));
    } else {
      const row = asRecord(entry);
      push(category, str(row.amount_usd) || str(row.amount) || str(row.total) || str(row.net), row.direction);
    }
  }
  return rows;
}

export function normalizeLedger(value: unknown): CashmaxxLedger {
  const record = asRecord(value);
  return {
    income: str(record.income) || "0",
    costs: str(record.costs) || "0",
    net: str(record.net) || "0",
    by_category: normalizeCategories(record.by_category),
  };
}

export function normalizeApproval(value: unknown): CashmaxxApproval {
  const row = asRecord(value);
  const payment = asRecord(row.payment);
  const pick = (...keys: string[]) => {
    for (const key of keys) {
      const found = str(row[key]) || str(payment[key]);
      if (found) return found;
    }
    return "";
  };
  return {
    id: pick("id", "approval_id"),
    amount_usd: pick("amount_usd", "amount") || "0",
    to: pick("to", "to_addr", "recipient"),
    purpose: pick("purpose"),
    reason: pick("reason"),
    ts: pick("ts", "created_at") || null,
    category: pick("category") || undefined,
  };
}

function summarizeEventData(data: unknown): string {
  let record = asRecord(data);
  if (typeof data === "string") {
    try {
      record = asRecord(JSON.parse(data));
    } catch {
      return data.slice(0, 120);
    }
  }
  const parts: string[] = [];
  const amount = str(record.amount_usd) || str(record.amount);
  if (amount) parts.push(`$${amount}`);
  for (const key of ["status", "decision", "reason", "purpose", "via"]) {
    const value = str(record[key]);
    if (value) parts.push(value);
  }
  return parts.join(" · ");
}

export function normalizeEvent(value: unknown, index: number): CashmaxxEvent {
  const row = asRecord(value);
  return {
    id: str(row.id) || `event-${index}`,
    ts: str(row.ts) || null,
    actor: str(row.actor),
    type: str(row.type) || "event",
    summary: summarizeEventData(row.data ?? row.data_json),
  };
}

function asStringList<T extends string>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((item) => typeof item === "string") as T[]) : [];
}

function nullableString(value: unknown): string | null {
  const text = str(value);
  return text ? text : null;
}

function int(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

export function normalizeSettings(value: unknown): CashmaxxSettings {
  const outer = asRecord(value);
  const record = asRecord(outer.settings).budgetUsd !== undefined ? asRecord(outer.settings) : outer;
  const mode = str(record.computePaymentMode) as ComputePaymentMode;
  return {
    network: str(record.network) || "base-sepolia",
    budgetUsd: str(record.budgetUsd) || "0",
    perTxApprovalUsd: str(record.perTxApprovalUsd) || "0",
    dailyCapUsd: str(record.dailyCapUsd) || "0",
    newRecipientNeedsApproval: record.newRecipientNeedsApproval !== false,
    maxPaymentsPerHour: int(record.maxPaymentsPerHour, 20),
    allowlist: asStringList<string>(record.allowlist),
    earningMethods: asStringList<EarningMethod>(record.earningMethods),
    rules: asStringList<SafetyRule>(record.rules),
    lossStopUsd: str(record.lossStopUsd) || "10",
    computePaymentMode: mode || "virtual",
    ownerWallet: nullableString(record.ownerWallet),
    reimburseIntervalHours: int(record.reimburseIntervalHours, 24),
    ownerTopupThresholdUsd: str(record.ownerTopupThresholdUsd) || "2",
    x402GatewayUrl: nullableString(record.x402GatewayUrl),
    publicPnl: record.publicPnl === true,
    frozen: record.frozen === true,
    frozenReason: nullableString(record.frozenReason),
    publicPnlUrl: nullableString(record.publicPnlUrl ?? outer.publicPnlUrl),
  };
}

// ---- endpoints ---------------------------------------------------------------------------

export async function fetchHealth(token: string): Promise<CashmaxxHealth> {
  const body = asRecord(await cashmaxxGet(token, "/health"));
  return {
    ok: body.ok !== false,
    network: str(body.network),
    frozen: body.frozen === true,
    version: str(body.version) || undefined,
  };
}

export async function fetchWallet(token: string): Promise<CashmaxxWallet> {
  const body = asRecord(await cashmaxxGet(token, "/wallet"));
  return {
    address: str(body.address),
    network: str(body.network),
    balance_usdc: str(body.balance_usdc) || "0",
    available_budget_usd: str(body.available_budget_usd) || "0",
  };
}

export async function fetchLedger(token: string, window: PnlWindow): Promise<CashmaxxLedger> {
  return normalizeLedger(await cashmaxxGet(token, "/ledger", { window }));
}

export async function fetchEvents(token: string, since?: string): Promise<CashmaxxEvent[]> {
  const body = await cashmaxxGet(token, "/events", { since });
  return listFrom(body, "events", "items").map(normalizeEvent);
}

export async function fetchPendingApprovals(token: string): Promise<CashmaxxApproval[]> {
  const body = await cashmaxxGet(token, "/approvals", { status: "pending" });
  return listFrom(body, "approvals", "items").map(normalizeApproval);
}

export async function fetchCashmaxxSettings(token: string): Promise<CashmaxxSettings> {
  return normalizeSettings(await cashmaxxGet(token, "/settings"));
}

export function approveApproval({ transport }: CashmaxxConnection, approvalId: string, ownerSession: string) {
  return cashmaxxMutation(transport, "cashmaxx.approve", { approval_id: approvalId, owner_session: ownerSession });
}

export function denyApproval({ transport }: CashmaxxConnection, approvalId: string, ownerSession: string) {
  return cashmaxxMutation(transport, "cashmaxx.deny", { approval_id: approvalId, owner_session: ownerSession });
}

export async function patchCashmaxxSettings(
  { transport }: CashmaxxConnection,
  patch: CashmaxxSettingsPatch,
  ownerSession: string,
): Promise<CashmaxxSettings | null> {
  const body = await cashmaxxMutation(transport, "cashmaxx.settings.update", {
    patch,
    owner_session: ownerSession,
  });
  const record = asRecord(body);
  const inner = asRecord(record.settings);
  return record.budgetUsd !== undefined || inner.budgetUsd !== undefined ? normalizeSettings(body) : null;
}

/** Anyone may pull the kill switch: no owner session needed. */
export function freezeGuard({ transport }: CashmaxxConnection, reason: string) {
  return cashmaxxMutation(transport, "cashmaxx.freeze", { reason });
}

export function unfreezeGuard({ transport }: CashmaxxConnection, ownerSession: string) {
  return cashmaxxMutation(transport, "cashmaxx.unfreeze", { owner_session: ownerSession });
}

export async function createOwnerSession({ transport }: CashmaxxConnection, pin: string): Promise<OwnerSession> {
  const body = asRecord(await cashmaxxMutation(transport, "cashmaxx.owner_session", { pin }));
  const session = str(body.session);
  if (!session) throw new CashmaxxError(502, "bad_response", "The guard did not return a session.");
  return { session, expires_at: str(body.expires_at) };
}
