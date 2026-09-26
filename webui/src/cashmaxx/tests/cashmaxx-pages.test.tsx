import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import QRCode from "qrcode";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CashmaxxSection, type CashmaxxPage } from "../CashmaxxSection";
import {
  FUTURE,
  mockCashmaxxFetch,
  mockCashmaxxTransport,
  settingsBody,
  type MutationRoute,
  type RecordedMutation,
  type Route,
} from "./test-utils";

const PENDING = [
  {
    id: "a1", ts: new Date(Date.now() - 5 * 60_000).toISOString(), reason: "over_threshold",
    amount_usd: "7.50", to: "0x1111111111111111111111111111111111111111", purpose: "Buy a domain",
  },
  {
    id: "a2", ts: new Date(Date.now() - 60 * 60_000).toISOString(), reason: "new_recipient",
    amount_usd: "1.00", to_addr: "0x2222222222222222222222222222222222222222", purpose: "Pay an API",
  },
];

beforeEach(() => {
  vi.spyOn(QRCode, "toDataURL").mockImplementation((async () => "data:image/png;base64,qr") as never);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setup(page: CashmaxxPage, reads: Route, mutations: MutationRoute = () => undefined) {
  const fetchState = mockCashmaxxFetch(reads);
  const transport = mockCashmaxxTransport(mutations);
  const user = userEvent.setup();
  render(<CashmaxxSection connection={transport.connection} initialPage={page} />);
  return { ...fetchState, ...transport, user };
}

function actions(mutations: RecordedMutation[], action: string) {
  return mutations.filter((call) => call.action === action);
}

async function enterPin(user: ReturnType<typeof userEvent.setup>, pin = "1234", expectClose = true) {
  const dialog = await screen.findByRole("dialog", { name: "Owner PIN" });
  await user.type(within(dialog).getByLabelText("PIN"), pin);
  await user.click(within(dialog).getByRole("button", { name: "Unlock" }));
  if (expectClose) {
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Owner PIN" })).not.toBeInTheDocument());
  }
}

const approvalsRead: Route = (call) =>
  call.path === "/approvals" && call.query.get("status") === "pending" ? { body: { approvals: PENDING } } : undefined;

describe("Cashmaxx approvals", () => {
  it("asks for the PIN once, keeps the session in memory only, and sends it on approve and deny", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { user, mutations, calls } = setup("approvals", approvalsRead, (call) => {
      if (call.action === "cashmaxx.owner_session") return { body: { session: "sess-secret", expires_at: FUTURE } };
      if (call.action === "cashmaxx.approve" || call.action === "cashmaxx.deny") return { body: { status: "ok" } };
      return undefined;
    });

    const first = await screen.findByTestId("cashmaxx-approval-a1");
    expect(within(first).getByText("Over threshold")).toBeInTheDocument();
    expect(within(first).getByText("Buy a domain")).toBeInTheDocument();
    expect(within(first).getByText("0x1111…1111")).toBeInTheDocument();
    expect(within(screen.getByTestId("cashmaxx-approval-a2")).getByText("New recipient")).toBeInTheDocument();
    expect(screen.getByText(/also available in the guard's Telegram bot/)).toBeInTheDocument();
    expect(calls[0].headers.authorization).toBe("Bearer webui-token");

    await user.click(within(first).getByRole("button", { name: "Approve" }));
    await enterPin(user);

    await waitFor(() => expect(actions(mutations, "cashmaxx.approve")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.owner_session")[0].payload).toEqual({ pin: "1234" });
    expect(actions(mutations, "cashmaxx.approve")[0].payload).toEqual({ approval_id: "a1", owner_session: "sess-secret" });

    // The second owner action reuses the in-memory session: no second PIN prompt.
    const second = screen.getByTestId("cashmaxx-approval-a2");
    await user.click(within(second).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.deny")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.deny")[0].payload).toEqual({ approval_id: "a2", owner_session: "sess-secret" });
    expect(actions(mutations, "cashmaxx.owner_session")).toHaveLength(1);
    expect(screen.queryByRole("dialog", { name: "Owner PIN" })).not.toBeInTheDocument();

    // Never persisted: neither the session nor the PIN reaches web storage.
    for (const [, value] of setItem.mock.calls) {
      expect(String(value)).not.toContain("sess-secret");
      expect(String(value)).not.toContain("1234");
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index) ?? "";
        expect(`${key}=${storage.getItem(key)}`).not.toContain("sess-secret");
      }
    }
    // Reads never carry the owner session.
    for (const call of calls) expect(call.headers["x-cashmaxx-owner"]).toBeUndefined();
  });

  it("asks for the PIN again when the guard rejects the session", async () => {
    let sessions = 0;
    const { user, mutations } = setup("approvals", (call) => (call.path === "/approvals" ? { body: [PENDING[0]] } : undefined), (call) => {
      if (call.action === "cashmaxx.owner_session") {
        sessions += 1;
        return { body: { session: `sess-${sessions}`, expires_at: FUTURE } };
      }
      if (call.action === "cashmaxx.approve") {
        return call.payload.owner_session === "sess-1"
          ? { status: 401, body: { error: "unauthorized", message: "session expired" } }
          : { body: { status: "paid" } };
      }
      return undefined;
    });
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await enterPin(user, "1234", false);
    await waitFor(() => expect(actions(mutations, "cashmaxx.approve")).toHaveLength(1));
    await enterPin(user, "5678");
    await waitFor(() => expect(actions(mutations, "cashmaxx.approve")).toHaveLength(2));
    expect(actions(mutations, "cashmaxx.approve")[1].payload.owner_session).toBe("sess-2");
  });

  it("shows a wrong-PIN error and stays open", async () => {
    const { user } = setup("approvals", approvalsRead, (call) =>
      call.action === "cashmaxx.owner_session" ? { status: 401, body: { error: "bad_pin", message: "bad pin" } } : undefined);
    await user.click((await screen.findAllByRole("button", { name: "Approve" }))[0]);
    await enterPin(user, "0000", false);
    const dialog = screen.getByRole("dialog", { name: "Owner PIN" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("That PIN is not correct.");
  });

  it("reports a policy failure from the guard", async () => {
    const { user } = setup("approvals", approvalsRead, (call) => {
      if (call.action === "cashmaxx.owner_session") return { body: { session: "s", expires_at: FUTURE } };
      if (call.action === "cashmaxx.approve") return { status: 409, body: { error: "frozen", message: "Guard is frozen." } };
      return undefined;
    });
    await user.click((await screen.findAllByRole("button", { name: "Approve" }))[0]);
    await enterPin(user);
    expect(await screen.findByText(/Spending is frozen\. Guard is frozen\./)).toBeInTheDocument();
  });

  it("shows an empty state when nothing is pending", async () => {
    setup("approvals", (call) => (call.path === "/approvals" ? { body: { approvals: [] } } : undefined));
    expect(await screen.findByText("Nothing waiting")).toBeInTheDocument();
  });

  it("does not send the approval when the PIN dialog is cancelled", async () => {
    const { user, mutations } = setup("approvals", approvalsRead);
    await user.click((await screen.findAllByRole("button", { name: "Approve" }))[0]);
    const dialog = await screen.findByRole("dialog", { name: "Owner PIN" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Owner PIN" })).not.toBeInTheDocument());
    expect(mutations).toHaveLength(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("polls every 10 seconds while visible", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { calls } = setup("approvals", approvalsRead);
      await screen.findByTestId("cashmaxx-approval-a1");
      const before = calls.length;
      await vi.advanceTimersByTimeAsync(10_000);
      await waitFor(() => expect(calls.length).toBe(before + 1));
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Cashmaxx guard unavailable", () => {
  it.each(["overview", "approvals", "settings"] as const)("shows the start-the-guard empty state on %s", async (page) => {
    setup(page, () => ({ status: 503, body: { error: "guard_unavailable", message: "Guard unreachable" } }));
    expect(await screen.findByText("Guard is not running")).toBeInTheDocument();
    expect(screen.getByText("cashmaxx guard")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("treats a network failure the same way", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const { connection } = mockCashmaxxTransport(() => undefined);
    render(<CashmaxxSection connection={connection} initialPage="overview" />);
    expect(await screen.findByText("Guard is not running")).toBeInTheDocument();
  });

  it("explains when Cashmaxx is not configured on the gateway", async () => {
    setup("overview", () => ({ status: 503, body: { error: "not_configured", message: "Cashmaxx is not configured" } }));
    expect(await screen.findByText("Cashmaxx is not set up")).toBeInTheDocument();
    expect(screen.getByText("cashmaxx onboard")).toBeInTheDocument();
  });

  it("recovers on retry once the guard is up", async () => {
    let up = false;
    const { user } = setup("approvals", (call) =>
      up ? approvalsRead(call) : { status: 503, body: { error: "guard_unavailable", message: "down" } });
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    up = true;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("cashmaxx-approval-a1")).toBeInTheDocument();
  });
});

describe("Cashmaxx overview", () => {
  const overviewReads = (frozen = false, network = "base-sepolia"): Route => (call) => {
    switch (call.path) {
      case "/health": return { body: { ok: true, network, frozen, version: "0.1.0" } };
      case "/wallet": return { body: {
        address: "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd", network,
        balance_usdc: "42.5", available_budget_usd: "37.25",
      } };
      case "/ledger": {
        const window = call.query.get("window");
        if (window === "30d") return { body: { income: "20", costs: "5", net: "15", by_category: { sale_stripe: "20", compute: "5" } } };
        if (window === "all") return { body: { income: "1", costs: "9", net: "-8", by_category: [] } };
        return { body: { income: "3", costs: "1", net: "2", by_category: [{ category: "sale_x402", direction: "income", amount_usd: "3" }] } };
      }
      case "/events": return { body: { events: [{ id: 1, ts: new Date().toISOString(), actor: "agent", type: "spend_request", data: { amount_usd: "1.00", status: "paid" } }] } };
      default: return undefined;
    }
  };

  it("shows the wallet, a testnet badge, and switches the P&L window", async () => {
    const { user, calls } = setup("overview", overviewReads());

    expect(await screen.findByTestId("cashmaxx-wallet-address")).toHaveTextContent("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd");
    expect(screen.getByRole("button", { name: "Copy wallet address" })).toBeInTheDocument();
    expect(screen.getByText("Base Sepolia · Testnet")).toBeInTheDocument();
    expect(screen.getByText("$42.50")).toBeInTheDocument();
    expect(screen.getByText("$37.25")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "QR code for the wallet address" })).toBeInTheDocument();
    expect(await screen.findByText("Spend request")).toBeInTheDocument();

    const pnl = screen.getByRole("region", { name: "Profit and loss" });
    expect(await within(pnl).findByText("x402 sales")).toBeInTheDocument();
    const windows = () => calls.filter((call) => call.path === "/ledger").map((call) => call.query.get("window"));
    expect(windows()).toEqual(["7d"]);

    await user.click(within(pnl).getByRole("button", { name: "30 days" }));
    expect(await within(pnl).findByText("Stripe sales")).toBeInTheDocument();
    expect(within(pnl).getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
    expect(within(pnl).getAllByText(/\+\$15\.00/).length).toBeGreaterThan(0);

    await user.click(within(pnl).getByRole("button", { name: "All time" }));
    expect(await within(pnl).findByText(/−\$8\.00/)).toBeInTheDocument();
    expect(within(pnl).getByText(/−\$8\.00/)).toHaveTextContent("(loss)");
    expect(within(pnl).getByText("No income or costs in this window yet.")).toBeInTheDocument();
    expect(windows()).toEqual(["7d", "30d", "all"]);
  });

  it("freezes after confirmation without a PIN", async () => {
    const { user, mutations } = setup("overview", overviewReads(), (call) =>
      call.action === "cashmaxx.freeze" ? { body: { frozen: true } } : undefined);
    await user.click(await screen.findByRole("button", { name: "Freeze" }));
    const dialog = await screen.findByRole("dialog", { name: "Freeze all spending?" });
    await user.type(within(dialog).getByLabelText("Reason (optional)"), "looks wrong");
    await user.click(within(dialog).getByRole("button", { name: "Freeze now" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.freeze")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.freeze")[0].payload).toEqual({ reason: "looks wrong" });
    expect(actions(mutations, "cashmaxx.owner_session")).toHaveLength(0);
  });

  it("shows the frozen banner and unfreezes with the PIN", async () => {
    const { user, mutations } = setup("overview", overviewReads(true, "base"), (call) => {
      if (call.action === "cashmaxx.owner_session") return { body: { session: "s", expires_at: FUTURE } };
      if (call.action === "cashmaxx.unfreeze") return { body: { frozen: false } };
      return undefined;
    });
    expect(await screen.findByText("Spending is frozen")).toBeInTheDocument();
    expect(screen.getByText("Base mainnet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Unfreeze (PIN)" }));
    await enterPin(user);
    await waitFor(() => expect(actions(mutations, "cashmaxx.unfreeze")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.unfreeze")[0].payload).toEqual({ owner_session: "s" });
  });
});

describe("Cashmaxx settings", () => {
  const settingsRead = (overrides: Record<string, unknown> = {}): Route => (call) =>
    call.path === "/settings" ? { body: settingsBody(overrides) } : undefined;

  it("sends only the changed fields with the owner session", async () => {
    let current = settingsBody();
    const { user, mutations } = setup("settings", (call) => (call.path === "/settings" ? { body: current } : undefined), (call) => {
      if (call.action === "cashmaxx.owner_session") return { body: { session: "owner-s", expires_at: FUTURE } };
      if (call.action === "cashmaxx.settings.update") {
        current = { ...current, ...(call.payload.patch as object) };
        return { body: current };
      }
      return undefined;
    });

    const budget = await screen.findByRole("textbox", { name: "Budget" });
    await user.clear(budget);
    await user.type(budget, "60");
    await user.click(screen.getByRole("switch", { name: "Publish P&L" }));
    await user.click(screen.getByRole("switch", { name: "No spam" }));
    await user.click(screen.getByRole("button", { name: "Save with PIN" }));
    await enterPin(user);

    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.settings.update")[0].payload).toEqual({
      owner_session: "owner-s",
      patch: {
        budgetUsd: "60",
        publicPnl: true,
        rules: ["bounty_review", "loss_stop", "no_trading"],
      },
    });
    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save with PIN" })).toBeDisabled();
  });

  it("shows the guard's validation error on 422", async () => {
    const { user } = setup("settings", settingsRead(), (call) => {
      if (call.action === "cashmaxx.owner_session") return { body: { session: "s", expires_at: FUTURE } };
      if (call.action === "cashmaxx.settings.update") return { status: 422, body: { error: "invalid", message: "dailyCapUsd too high" } };
      return undefined;
    });
    const cap = await screen.findByRole("textbox", { name: "Daily spending cap" });
    await user.clear(cap);
    await user.type(cap, "11");
    await user.click(screen.getByRole("button", { name: "Save with PIN" }));
    await enterPin(user);
    expect(await screen.findByText(/The guard rejected this change\. dailyCapUsd too high/)).toBeInTheDocument();
  });

  it("does not count a reformatted but equal amount as a change", async () => {
    const { user } = setup("settings", settingsRead());
    const budget = await screen.findByRole("textbox", { name: "Budget" });
    await user.clear(budget);
    await user.type(budget, "50.00");
    expect(screen.getByRole("button", { name: "Save with PIN" })).toBeDisabled();
  });

  it("shows the conditional fields for each compute mode", async () => {
    const { user } = setup("settings", settingsRead());
    await screen.findByRole("textbox", { name: "Budget" });

    const none = () => {
      expect(screen.queryByRole("textbox", { name: "Owner wallet" })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "Reimburse every" })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "Top-up threshold" })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "x402 gateway URL" })).not.toBeInTheDocument();
    };
    expect(screen.getByRole("radio", { name: /^Virtual/ })).toBeChecked();
    expect(screen.getByText(/Both the OpenRouter account and the wallet belong to you/)).toBeInTheDocument();
    none();

    await user.click(screen.getByRole("radio", { name: /^Reimburse/ }));
    expect(screen.getByRole("textbox", { name: "Owner wallet" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Reimburse every" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Top-up threshold" })).not.toBeInTheDocument();
    expect(screen.getByText("Reimburse needs the owner wallet address.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save with PIN" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Owner wallet" }), "0x12");
    expect(screen.getByText("Use a wallet address: 0x followed by 40 hex characters.")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /^Owner top-up/ }));
    expect(screen.getByRole("textbox", { name: "Top-up threshold" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Owner wallet" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /^x402 gateway/ }));
    expect(screen.getByRole("textbox", { name: "x402 gateway URL" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Top-up threshold" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /^Virtual/ }));
    none();
  });

  it("hides the loss stop amount when the loss stop rule is off, and only warns on caps", async () => {
    const { user } = setup("settings", settingsRead());
    expect(await screen.findByRole("textbox", { name: "Loss stop at" })).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Loss stop" }));
    expect(screen.queryByRole("textbox", { name: "Loss stop at" })).not.toBeInTheDocument();

    const perTx = screen.getByRole("textbox", { name: "Approval threshold per payment" });
    await user.clear(perTx);
    await user.type(perTx, "20");
    expect(screen.getByText(/above the daily cap/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save with PIN" })).toBeEnabled();
  });

  it("shows the public URL only when public P&L is on", async () => {
    const { user } = setup("settings", settingsRead({ publicPnlUrl: "https://pnl.example.com/public/pnl" }));
    await screen.findByRole("switch", { name: "Publish P&L" });
    expect(screen.queryByText("https://pnl.example.com/public/pnl")).not.toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Publish P&L" }));
    expect(screen.getByText("https://pnl.example.com/public/pnl")).toBeInTheDocument();
  });

  it("edits the allowlist and rejects malformed entries", async () => {
    const { user } = setup("settings", settingsRead({ allowlist: ["api.example.com"] }));
    const input = await screen.findByRole("textbox", { name: "Add allowlist entry" });
    await user.type(input, "0xnothex{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent("Use a wallet address (0x + 40 hex characters) or a host name.");
    await user.clear(input);
    await user.type(input, "0x3333333333333333333333333333333333333333{Enter}");
    const list = screen.getByRole("list", { name: "Allowlist" });
    expect(within(list).getByText("0x3333333333333333333333333333333333333333")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove api.example.com" }));
    expect(within(list).queryByText("api.example.com")).not.toBeInTheDocument();
  });
});
