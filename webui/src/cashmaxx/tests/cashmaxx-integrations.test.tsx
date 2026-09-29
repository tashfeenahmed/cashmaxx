import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CashmaxxSection } from "../CashmaxxSection";
import {
  FUTURE,
  mockCashmaxxFetch,
  mockCashmaxxTransport,
  settingsBody,
  type MutationRoute,
  type RecordedMutation,
  type Route,
} from "./test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const DAY = 86_400_000;
const NINE_DAYS_AGO = new Date(Date.now() - 9 * DAY).toISOString();
const HOUR_AGO = new Date(Date.now() - 3600_000).toISOString();

function f(name: string, label: string, extra: Record<string, unknown> = {}) {
  return { name, label, secret: false, required: true, placeholder: "", choices: [], set: false, ...extra };
}

function fullItems() {
  return [
    {
      id: "openrouter", label: "OpenRouter", kind: "guard", category: "compute", connected: true,
      summary: "Runs the model.", docs_url: "https://openrouter.ai/settings/keys",
      fields: [f("apiKey", "API key", { secret: true, set: true })],
      connected_at: "2026-09-01T00:00:00Z", last_test: { ok: true, message: "key ok", at: HOUR_AGO },
    },
    {
      id: "cdp", label: "Coinbase CDP wallet", kind: "guard", category: "money", connected: false,
      summary: "Wallet.", docs_url: "https://portal.cdp.coinbase.com",
      fields: [f("apiKeyId", "API key id"), f("apiKeySecret", "API key secret", { secret: true }),
        f("walletSecret", "Wallet secret", { secret: true })],
      connected_at: null, last_test: null,
    },
    {
      id: "gmail", label: "Gmail", kind: "guard", category: "email", connected: true,
      summary: "Gmail.", docs_url: "https://myaccount.google.com/apppasswords",
      fields: [f("address", "Gmail address", { set: true, value: "agent@example.com" }),
        f("appPassword", "App password", { secret: true, set: true })],
      connected_at: NINE_DAYS_AGO, last_test: null,
    },
    {
      id: "agentmail", label: "AgentMail", kind: "guard", category: "email", connected: false,
      summary: "AgentMail.", docs_url: "https://console.agentmail.to",
      fields: [f("apiKey", "API key", { secret: true }), f("inboxId", "Inbox address", { required: false })],
      connected_at: null, last_test: null,
    },
    {
      id: "bluesky", label: "Bluesky", kind: "guard", category: "social", connected: true,
      summary: "Bluesky.", docs_url: "https://bsky.app/settings/app-passwords",
      fields: [f("handle", "Handle", { set: true, value: "agent.bsky.social" }), f("appPassword", "App password", { secret: true, set: true })],
      connected_at: "2026-09-01T00:00:00Z", last_test: { ok: false, message: "Invalid identifier or password", at: HOUR_AGO },
    },
    {
      id: "hosting", label: "Public hosting", kind: "guard", category: "hosting", connected: false,
      summary: "Hosting.", docs_url: "https://developers.cloudflare.com/",
      fields: [f("provider", "Provider", { choices: ["cloudflare_quick", "cloudflare_token"] }),
        f("tunnelToken", "Tunnel token", { secret: true, required: false })],
      connected_at: null, last_test: null,
    },
    {
      id: "browser", label: "Browser", kind: "agent", category: "browser", connected: false,
      summary: "Browser.", docs_url: "https://playwright.dev/",
      fields: [f("provider", "Provider", { choices: ["playwright", "browserbase"] })],
      connected_at: null, last_test: null,
    },
  ];
}

function reducedItems() {
  return fullItems().map(({ id, label, kind, category, connected }) => ({ id, label, kind, category, connected }));
}

interface State {
  settings: Record<string, unknown>;
  items: ReturnType<typeof fullItems>;
}

function setup(mutations: MutationRoute = () => undefined, settingsOverrides: Record<string, unknown> = {}) {
  const state: State = { settings: settingsBody(settingsOverrides), items: fullItems() };
  const reads: Route = (call) => {
    if (call.path === "/integrations") return { body: { integrations: reducedItems() } };
    if (call.path === "/settings") return { body: state.settings };
    if (call.path === "/approvals") return { body: { approvals: [] } };
    return undefined;
  };
  const fetchState = mockCashmaxxFetch(reads);
  const transport = mockCashmaxxTransport((call) => {
    const custom = mutations(call);
    if (custom) return custom;
    if (call.action === "cashmaxx.owner_session") return { body: { session: "owner-s", expires_at: FUTURE } };
    if (call.action === "cashmaxx.integrations.list") return { body: { integrations: state.items } };
    if (call.action === "cashmaxx.settings.update") {
      state.settings = { ...state.settings, ...(call.payload.patch as object) };
      return { body: state.settings };
    }
    return undefined;
  });
  const user = userEvent.setup();
  render(<CashmaxxSection connection={transport.connection} initialPage="integrations" />);
  return { ...fetchState, ...transport, user, state };
}

function actions(mutations: RecordedMutation[], action: string) {
  return mutations.filter((call) => call.action === action);
}

async function enterPin(user: ReturnType<typeof userEvent.setup>, pin = "1234") {
  const dialog = await screen.findByRole("dialog", { name: "Owner PIN" });
  await user.type(within(dialog).getByLabelText("PIN"), pin);
  await user.click(within(dialog).getByRole("button", { name: "Unlock" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Owner PIN" })).not.toBeInTheDocument());
}

async function unlocked(mutations?: MutationRoute, settingsOverrides?: Record<string, unknown>) {
  const ctx = setup(mutations, settingsOverrides);
  await ctx.user.click(await screen.findByRole("button", { name: "Unlock to manage" }));
  await enterPin(ctx.user);
  await screen.findByRole("button", { name: /^Edit OpenRouter/ });
  return ctx;
}

const card = (id: string) => screen.getByTestId(`cashmaxx-integration-${id}`);

describe("Cashmaxx integrations: before the PIN", () => {
  it("renders statuses from the plain listing, grouped by category, with no owner calls", async () => {
    const { mutations, calls } = setup();
    expect(await screen.findByRole("tab", { name: "Integrations" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Overview", "Approvals", "Integrations", "Settings"]);
    await screen.findByTestId("cashmaxx-integration-openrouter");

    const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(headings).toEqual(expect.arrayContaining(["Money", "Compute", "Email", "Social", "Hosting", "Browser"]));
    expect(headings.indexOf("Money")).toBeLessThan(headings.indexOf("Compute"));
    expect(headings.indexOf("Email")).toBeLessThan(headings.indexOf("Social"));

    expect(within(card("openrouter")).getByText("Connected")).toBeInTheDocument();
    expect(within(card("cdp")).getByText("Not connected")).toBeInTheDocument();
    // The plain listing has no test history, so a failed test is not shown yet.
    expect(within(card("bluesky")).getByText("Connected")).toBeInTheDocument();
    expect(within(card("gmail")).getByText(/read the inbox and send within the daily cap/)).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Unlock to manage" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Connect/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Test/ })).not.toBeInTheDocument();
    expect(mutations).toHaveLength(0);
    expect(calls.map((call) => call.path)).toEqual(["/integrations"]);
    expect(calls[0].headers.authorization).toBe("Bearer webui-token");
  });

  it("asks for the PIN on unlock, then loads the owner listing with the session", async () => {
    const { user, mutations } = setup();
    await user.click(await screen.findByRole("button", { name: "Unlock to manage" }));
    await enterPin(user);
    await waitFor(() => expect(actions(mutations, "cashmaxx.integrations.list")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.owner_session")[0].payload).toEqual({ pin: "1234" });
    expect(actions(mutations, "cashmaxx.integrations.list")[0].payload).toEqual({ owner_session: "owner-s" });

    expect(await within(card("bluesky")).findByText("Test failed", { selector: "span" })).toBeInTheDocument();
    expect(within(card("bluesky")).getByText(/Invalid identifier or password/)).toBeInTheDocument();
    expect(within(card("openrouter")).getByText(/Test passed/)).toBeInTheDocument();
    expect(within(card("cdp")).getByText("Not tested yet")).toBeInTheDocument();
    expect(within(card("openrouter")).getByRole("link", { name: /Docs/ })).toHaveAttribute("href", "https://openrouter.ai/settings/keys");
    expect(within(card("cdp")).getByRole("button", { name: /^Connect/ })).toBeInTheDocument();
    expect(within(card("cdp")).queryByRole("button", { name: /^Test/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unlock to manage" })).not.toBeInTheDocument();
  });

  it("stays locked when the PIN prompt is cancelled", async () => {
    const { user, mutations } = setup();
    await user.click(await screen.findByRole("button", { name: "Unlock to manage" }));
    const dialog = await screen.findByRole("dialog", { name: "Owner PIN" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Owner PIN" })).not.toBeInTheDocument());
    expect(mutations).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Unlock to manage" })).toBeInTheDocument();
  });

  it("reuses the session when coming back to the tab", async () => {
    const { user, mutations } = await unlocked();
    await user.click(screen.getByRole("tab", { name: "Approvals" }));
    await screen.findByText("Nothing waiting");
    await user.click(screen.getByRole("tab", { name: "Integrations" }));
    await screen.findByRole("button", { name: /^Edit OpenRouter/ });
    expect(actions(mutations, "cashmaxx.integrations.list")).toHaveLength(2);
    expect(actions(mutations, "cashmaxx.owner_session")).toHaveLength(1);
  });
});

describe("Cashmaxx integrations: connect and edit", () => {
  it("validates required fields before sending", async () => {
    const { user, mutations } = await unlocked((call) =>
      call.action === "cashmaxx.integrations.update" ? { body: { ...fullItems()[1], connected: true } } : undefined);
    await user.click(within(card("cdp")).getByRole("button", { name: /^Connect/ }));
    const dialog = await screen.findByRole("dialog", { name: "Connect Coinbase CDP wallet" });
    expect(within(dialog).getByLabelText(/API key secret/)).toHaveAttribute("type", "password");
    expect(within(dialog).getByLabelText(/^API key ID/)).toHaveAttribute("type", "text");

    await user.click(within(dialog).getByRole("button", { name: "Save with PIN" }));
    expect(within(dialog).getAllByText("This field is required.")).toHaveLength(3);
    expect(actions(mutations, "cashmaxx.integrations.update")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText(/^API key ID/), "key-id");
    await user.type(within(dialog).getByLabelText(/API key secret/), "key-secret");
    expect(within(dialog).getAllByText("This field is required.")).toHaveLength(1);
    await user.type(within(dialog).getByLabelText(/Wallet secret/), "wallet-secret");
    await user.click(within(dialog).getByRole("button", { name: "Save with PIN" }));

    await waitFor(() => expect(actions(mutations, "cashmaxx.integrations.update")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.integrations.update")[0].payload).toEqual({
      id: "cdp",
      owner_session: "owner-s",
      fields: { apiKeyId: "key-id", apiKeySecret: "key-secret", walletSecret: "wallet-secret" },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(within(card("cdp")).getByText(/Coinbase CDP wallet saved/)).toBeInTheDocument();
    expect(actions(mutations, "cashmaxx.owner_session")).toHaveLength(1);
  });

  it("never sends a stored secret left empty", async () => {
    const { user, mutations } = await unlocked((call) =>
      call.action === "cashmaxx.integrations.update" ? { body: fullItems()[2] } : undefined);
    await user.click(within(card("gmail")).getByRole("button", { name: /^Edit Gmail/ }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Gmail" });
    expect(within(dialog).getByText("Saved. Leave empty to keep it.")).toBeInTheDocument();
    const password = within(dialog).getByLabelText(/App password/);
    expect(password).toHaveValue("");
    const address = within(dialog).getByLabelText(/Gmail address/);
    expect(address).toHaveValue("agent@example.com");
    await user.clear(address);
    await user.type(address, "new@example.com");
    await user.click(within(dialog).getByRole("button", { name: "Save with PIN" }));

    await waitFor(() => expect(actions(mutations, "cashmaxx.integrations.update")).toHaveLength(1));
    const payload = actions(mutations, "cashmaxx.integrations.update")[0].payload;
    expect(payload.fields).toEqual({ address: "new@example.com" });
    expect(payload.fields).not.toHaveProperty("appPassword");
  });

  it("renders choices as a select and shows the guard's rejection", async () => {
    const { user, mutations } = await unlocked((call) =>
      call.action === "cashmaxx.integrations.update"
        ? { status: 400, body: { error: "invalid", message: "tunnelToken is required for cloudflare_token" } }
        : undefined);
    await user.click(within(card("hosting")).getByRole("button", { name: /^Connect/ }));
    const dialog = await screen.findByRole("dialog", { name: "Connect Public hosting" });
    const select = within(dialog).getByRole("combobox", { name: /Provider/ });
    expect(select).toHaveValue("cloudflare_quick");
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Cloudflare quick tunnel", "Cloudflare tunnel (token)",
    ]);
    await user.selectOptions(select, "cloudflare_token");
    await user.click(within(dialog).getByRole("button", { name: "Save with PIN" }));
    expect(await within(dialog).findByText("tunnelToken is required for cloudflare_token")).toBeInTheDocument();
    expect(actions(mutations, "cashmaxx.integrations.update")[0].payload.fields).toEqual({ provider: "cloudflare_token" });
  });
});

describe("Cashmaxx integrations: test and disconnect", () => {
  it("tests an integration and shows the result", async () => {
    const { user, mutations } = await unlocked((call) => {
      if (call.action !== "cashmaxx.integrations.test") return undefined;
      return call.payload.id === "openrouter"
        ? { body: { ok: true, message: "Key has $4.20 left" } }
        : { body: { ok: false, message: "Invalid identifier or password" } };
    });
    await user.click(within(card("openrouter")).getByRole("button", { name: /^Test OpenRouter/ }));
    expect(await within(card("openrouter")).findByText("OpenRouter: test passed. Key has $4.20 left")).toBeInTheDocument();
    expect(actions(mutations, "cashmaxx.integrations.test")[0].payload).toEqual({ id: "openrouter", owner_session: "owner-s" });

    await user.click(within(card("bluesky")).getByRole("button", { name: /^Test Bluesky/ }));
    const alert = await within(card("bluesky")).findByRole("alert");
    expect(alert).toHaveTextContent("Bluesky: test failed. Invalid identifier or password");
    // The listing is reloaded after each test so the last-test line is fresh.
    expect(actions(mutations, "cashmaxx.integrations.list")).toHaveLength(3);
  });

  it("disconnects only after confirmation", async () => {
    const { user, mutations, state } = await unlocked((call) => {
      if (call.action !== "cashmaxx.integrations.remove") return undefined;
      state.items = state.items.map((item) => (item.id === call.payload.id ? { ...item, connected: false } : item));
      return { body: { ok: true } };
    });
    await user.click(within(card("bluesky")).getByRole("button", { name: /^Disconnect Bluesky/ }));
    let dialog = await screen.findByRole("dialog", { name: "Disconnect Bluesky?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(actions(mutations, "cashmaxx.integrations.remove")).toHaveLength(0);

    await user.click(within(card("bluesky")).getByRole("button", { name: /^Disconnect Bluesky/ }));
    dialog = await screen.findByRole("dialog", { name: "Disconnect Bluesky?" });
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.integrations.remove")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.integrations.remove")[0].payload).toEqual({ id: "bluesky", owner_session: "owner-s" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await within(card("bluesky")).findByText("Bluesky disconnected.")).toBeInTheDocument();
    expect(within(card("bluesky")).getByText("Not connected")).toBeInTheDocument();
  });
});

describe("Cashmaxx integrations: related settings", () => {
  it("switches the email provider through the settings update action", async () => {
    const { user, mutations } = await unlocked();
    const group = await screen.findByRole("group", { name: "Sending provider" });
    expect(within(group).getByRole("radio", { name: "None" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "AgentMail" })).toBeDisabled();
    await user.click(within(group).getByRole("radio", { name: "Gmail" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.settings.update")[0].payload).toEqual({
      patch: { emailProvider: "gmail" }, owner_session: "owner-s",
    });
    await waitFor(() => expect(within(group).getByRole("radio", { name: "Gmail" })).toBeChecked());
  });

  it("says the daily cap is the limit when the next warm-up step would not raise it", async () => {
    await unlocked(undefined, { emailDailyCap: 20 });
    const line = await within(card("gmail")).findByTestId("cashmaxx-gmail-warmup");
    expect(line).toHaveTextContent("Week 2 of 4 (day 10): up to 20 emails today, set by your daily cap.");
  });

  it("shows the Gmail warm-up progress and saves the warm-up, caps and hosting switches", async () => {
    const { user, mutations } = await unlocked();
    const gmail = card("gmail");
    const line = await within(gmail).findByTestId("cashmaxx-gmail-warmup");
    expect(line).toHaveTextContent(/^Week 2 of 4 \(day 10\): up to 20 emails today, 40\/day from /);

    await user.click(within(gmail).getByRole("switch", { name: "Gmail warm-up" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(1));
    expect(actions(mutations, "cashmaxx.settings.update")[0].payload.patch).toEqual({ emailWarmup: false });
    await waitFor(() => expect(line).toHaveTextContent("Warm-up off: up to 50 emails a day."));

    const social = screen.getByRole("textbox", { name: "Daily post cap" });
    await user.clear(social);
    await user.type(social, "500");
    expect(screen.getByText("Enter a whole number from 0 to 100.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Save Daily post cap/ })).toBeDisabled();
    await user.clear(social);
    await user.type(social, "5");
    await user.click(screen.getByRole("button", { name: /^Save Daily post cap/ }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(2));
    expect(actions(mutations, "cashmaxx.settings.update")[1].payload.patch).toEqual({ socialDailyCap: 5 });

    const emailCap = screen.getByRole("textbox", { name: "Daily email cap" });
    await user.clear(emailCap);
    await user.type(emailCap, "30{Enter}");
    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(3));
    expect(actions(mutations, "cashmaxx.settings.update")[2].payload.patch).toEqual({ emailDailyCap: 30 });

    await user.click(within(card("hosting")).getByRole("switch", { name: "Allow public hosting" }));
    await waitFor(() => expect(actions(mutations, "cashmaxx.settings.update")).toHaveLength(4));
    expect(actions(mutations, "cashmaxx.settings.update")[3].payload.patch).toEqual({ hostingEnabled: true });
    expect(actions(mutations, "cashmaxx.owner_session")).toHaveLength(1);
  });
});
