import { vi } from "vitest";

import type { CashmaxxConnection } from "../api";

export interface RecordedCall {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
}

export type Route = (call: RecordedCall) => { status?: number; body?: unknown } | undefined;

function headersToRecord(headers: HeadersInit | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!headers) return out;
  new Headers(headers).forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}

/** Stubs global fetch with a router over /api/cashmaxx/* and records every call. */
export function mockCashmaxxFetch(route: Route) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const call: RecordedCall = {
      method: (init?.method ?? "GET").toUpperCase(),
      path: url.pathname.replace(/^\/api\/cashmaxx/, ""),
      query: url.searchParams,
      headers: headersToRecord(init?.headers),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const result = route(call) ?? { status: 404, body: { error: "not_found", message: call.path } };
    return new Response(JSON.stringify(result.body ?? {}), {
      status: result.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export interface RecordedMutation {
  action: string;
  payload: Record<string, unknown>;
}

export type MutationRoute = (call: RecordedMutation) => { status?: number; body?: unknown } | undefined;

/**
 * A fake WebUI socket transport. Failures reject the way `NanobotClient.requestMutation` does:
 * an Error with `status`, whose message is the proxy's raw JSON body.
 */
export function mockCashmaxxTransport(route: MutationRoute, token = "webui-token") {
  const mutations: RecordedMutation[] = [];
  const requestMutation = vi.fn(async (action: string, payload: Record<string, unknown> = {}) => {
    const call = { action, payload };
    mutations.push(call);
    const result = route(call) ?? { status: 404, body: { error: "not_found", message: action } };
    const status = result.status ?? 200;
    if (status >= 400) {
      throw Object.assign(new Error(JSON.stringify(result.body ?? {})), { status });
    }
    return result.body;
  });
  const connection: CashmaxxConnection = {
    token,
    transport: { requestMutation: requestMutation as CashmaxxConnection["transport"]["requestMutation"] },
  };
  return { mutations, requestMutation, connection };
}

export const FUTURE = new Date(Date.now() + 12 * 3600 * 1000).toISOString();

export function settingsBody(overrides: Record<string, unknown> = {}) {
  return {
    network: "base-sepolia",
    budgetUsd: "50",
    perTxApprovalUsd: "5",
    dailyCapUsd: "10",
    newRecipientNeedsApproval: true,
    maxPaymentsPerHour: 20,
    allowlist: [],
    earningMethods: ["digital_products", "x402_apis", "bounties", "agent_marketplaces"],
    rules: ["no_trading", "no_spam", "bounty_review", "loss_stop"],
    lossStopUsd: "10",
    computePaymentMode: "virtual",
    ownerWallet: null,
    reimburseIntervalHours: 24,
    ownerTopupThresholdUsd: "2",
    x402GatewayUrl: null,
    publicPnl: false,
    frozen: false,
    frozenReason: null,
    emailProvider: "none",
    emailDailyCap: 50,
    emailWarmup: true,
    socialDailyCap: 3,
    hostingEnabled: false,
    ...overrides,
  };
}
