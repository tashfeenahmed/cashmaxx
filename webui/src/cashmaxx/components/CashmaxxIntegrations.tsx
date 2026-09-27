import {
  AppWindow,
  AtSign,
  Cpu,
  CreditCard,
  ExternalLink,
  FlaskConical,
  GitPullRequest,
  Globe,
  Inbox,
  Loader2,
  Lock,
  Mail,
  Megaphone,
  MessagesSquare,
  Pencil,
  Plug,
  Search,
  Send,
  Unplug,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  fetchCashmaxxSettings,
  fetchIntegrationSummaries,
  listIntegrations,
  patchCashmaxxSettings,
  removeIntegration,
  testIntegration,
  type CashmaxxConnection,
} from "../api";
import { formatDateTime, formatRelativeTime } from "../format";
import { useCashmaxxQuery } from "../hooks";
import { groupIntegrations, integrationStatus, type IntegrationStatus } from "../integrations-model";
import { isPromptCancelled, useOwnerSession } from "../owner-session";
import type {
  CashmaxxSettings,
  CashmaxxSettingsPatch,
  EmailProvider,
  IntegrationItem,
  IntegrationSummary,
} from "../types";
import { ConnectIntegrationDialog, DisconnectIntegrationDialog } from "./IntegrationDialogs";
import { CapEditor, EmailProviderSwitch, GmailWarmup, SettingsUnavailable } from "./IntegrationSettings";
import { CashmaxxErrorState, errorMessage, InlineNotice } from "./shared";

const ICONS: Record<string, LucideIcon> = {
  openrouter: Cpu,
  cdp: Wallet,
  stripe: CreditCard,
  telegram: Send,
  gmail: Mail,
  agentmail: Inbox,
  bluesky: AtSign,
  x: Megaphone,
  reddit: MessagesSquare,
  hosting: Globe,
  browser: AppWindow,
  github: GitPullRequest,
  search: Search,
};

type Notice = { scope: string; tone: "error" | "info"; text: string };

/** The Integrations tab: statuses for everyone, connect/test/disconnect behind the owner PIN. */
export function CashmaxxIntegrations({ connection }: { connection: CashmaxxConnection }) {
  const { token } = connection;
  const { t } = useTranslation();
  const { runAsOwner, hasSession } = useOwnerSession();
  const summaries = useCashmaxxQuery(() => fetchIntegrationSummaries(token), `integrations:${token}`);
  const [items, setItems] = useState<IntegrationItem[] | null>(null);
  const [settings, setSettings] = useState<CashmaxxSettings | null>(null);
  const [settingsError, setSettingsError] = useState<unknown>(null);
  const [unlocking, setUnlocking] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [editing, setEditing] = useState<IntegrationItem | null>(null);
  const [removing, setRemoving] = useState<IntegrationItem | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await fetchCashmaxxSettings(token));
      setSettingsError(null);
    } catch (err) {
      setSettingsError(err);
    }
  }, [token]);

  const reloadItems = useCallback(async () => {
    const next = await runAsOwner((session) => listIntegrations(connection, session));
    setItems(next);
    return next;
  }, [connection, runAsOwner]);

  const unlock = useCallback(async () => {
    setUnlocking(true);
    setNotice(null);
    try {
      await Promise.all([reloadItems(), loadSettings()]);
    } catch (err) {
      if (!isPromptCancelled(err)) setNotice({ scope: "page", tone: "error", text: errorMessage(err, t) });
    } finally {
      setUnlocking(false);
    }
  }, [loadSettings, reloadItems, t]);

  // A session opened on another tab (Approvals, Settings) unlocks this one without a prompt.
  const autoUnlocked = useRef(false);
  useEffect(() => {
    if (hasSession && !items && !autoUnlocked.current) {
      autoUnlocked.current = true;
      void unlock();
    }
  }, [hasSession, items, unlock]);

  const runAction = async (scope: string, key: string, action: () => Promise<Notice | null>) => {
    setActing(key);
    setNotice(null);
    try {
      const result = await action();
      if (result) setNotice(result);
    } catch (err) {
      if (!isPromptCancelled(err)) setNotice({ scope, tone: "error", text: errorMessage(err, t) });
    } finally {
      setActing(null);
    }
  };

  const runTest = (item: IntegrationItem) =>
    runAction(item.id, `test:${item.id}`, async () => {
      const result = await runAsOwner((session) => testIntegration(connection, item.id, session));
      await reloadItems().catch(() => undefined);
      return {
        scope: item.id,
        tone: result.ok ? "info" : "error",
        text: t(result.ok ? "cashmaxx.integrations.testPassed" : "cashmaxx.integrations.testFailedMessage", {
          label: item.label,
          message: result.message,
        }).trim(),
      };
    });

  const disconnect = async (item: IntegrationItem) => {
    await runAction(item.id, `remove:${item.id}`, async () => {
      await runAsOwner((session) => removeIntegration(connection, item.id, session));
      setRemoving(null);
      await Promise.all([reloadItems().catch(() => undefined), loadSettings()]);
      return { scope: item.id, tone: "info", text: t("cashmaxx.integrations.disconnected", { label: item.label }) };
    });
  };

  const saveSettings = (scope: string, patch: CashmaxxSettingsPatch) =>
    runAction(scope, `settings:${scope}`, async () => {
      const updated = await runAsOwner((session) => patchCashmaxxSettings(connection, patch, session));
      setSettings(updated ?? await fetchCashmaxxSettings(token));
      return { scope, tone: "info", text: t("cashmaxx.integrations.settingSaved") };
    });

  const noticeFor = (scope: string) =>
    notice?.scope === scope ? <InlineNotice tone={notice.tone}>{notice.text}</InlineNotice> : null;

  if (!items) {
    if (!summaries.data) {
      if (summaries.error) return <CashmaxxErrorState error={summaries.error} onRetry={() => void summaries.reload()} />;
      return <Loading />;
    }
    return (
      <div className="settings-stack">
        <div className="flex flex-col gap-3 rounded-panel bg-settings-surface px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            <div className="min-w-0">
              <p className="text-[14px] font-medium text-foreground">{t("cashmaxx.integrations.lockedTitle")}</p>
              <p className="text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.integrations.lockedBody")}</p>
            </div>
          </div>
          <Button type="button" size="sm" disabled={unlocking} onClick={() => void unlock()} className="shrink-0 rounded-full">
            {unlocking ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {t("cashmaxx.integrations.unlock")}
          </Button>
        </div>
        {noticeFor("page")}
        <IntegrationGroups items={summaries.data} render={(item) => <IntegrationCard item={item} />} />
      </div>
    );
  }

  const connectedIds = new Set(items.filter((item) => item.connected).map((item) => item.id));
  const busy = acting !== null;

  const groupExtra = (category: string): ReactNode => {
    if (category === "email") {
      return (
        <GroupPanel title={t("cashmaxx.integrations.email.title")}>
          {settings ? (
            <div className="grid gap-4">
              <EmailProviderSwitch
                value={settings.emailProvider}
                connected={connectedIds}
                disabled={busy}
                onChange={(emailProvider: EmailProvider) => void saveSettings("email", { emailProvider })}
              />
              <CapEditor
                id="emailDailyCap"
                label={t("cashmaxx.integrations.email.dailyCap")}
                hint={t("cashmaxx.integrations.email.dailyCapHint")}
                value={settings.emailDailyCap}
                min={0}
                max={2000}
                disabled={busy}
                onSave={(emailDailyCap) => void saveSettings("email", { emailDailyCap })}
              />
            </div>
          ) : <SettingsUnavailable error={settingsError} onRetry={() => void loadSettings()} />}
          {noticeFor("email")}
        </GroupPanel>
      );
    }
    if (category === "social") {
      return (
        <GroupPanel title={t("cashmaxx.integrations.social.title")}>
          {settings ? (
            <CapEditor
              id="socialDailyCap"
              label={t("cashmaxx.integrations.social.dailyCap")}
              hint={t("cashmaxx.integrations.social.dailyCapHint")}
              value={settings.socialDailyCap}
              min={0}
              max={100}
              disabled={busy}
              onSave={(socialDailyCap) => void saveSettings("social", { socialDailyCap })}
            />
          ) : <SettingsUnavailable error={settingsError} onRetry={() => void loadSettings()} />}
          {noticeFor("social")}
        </GroupPanel>
      );
    }
    return null;
  };

  const cardExtra = (item: IntegrationItem): ReactNode => {
    if (!settings) return null;
    if (item.id === "gmail") {
      return (
        <GmailWarmup
          connectedAt={item.connected ? item.connected_at : null}
          warmup={settings.emailWarmup}
          dailyCap={settings.emailDailyCap}
          disabled={busy}
          onToggle={(emailWarmup) => void saveSettings(item.id, { emailWarmup })}
        />
      );
    }
    if (item.id === "hosting") {
      const title = t("cashmaxx.integrations.hosting.enabled");
      return (
        <div className="flex items-start justify-between gap-4 rounded-control bg-background/60 px-3.5 py-3">
          <div className="min-w-0">
            <p className="text-[13.5px] font-medium text-foreground">{title}</p>
            <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.integrations.hosting.enabledHint")}</p>
          </div>
          <ToggleButton
            checked={settings.hostingEnabled}
            disabled={busy}
            ariaLabel={title}
            label={settings.hostingEnabled ? t("settings.values.on") : t("settings.values.off")}
            onChange={(hostingEnabled) => void saveSettings(item.id, { hostingEnabled })}
          />
        </div>
      );
    }
    return null;
  };

  return (
    <div className="settings-stack">
      {noticeFor("page")}
      <IntegrationGroups
        items={items}
        extra={groupExtra}
        render={(item) => (
          <IntegrationCard item={item}>
            {cardExtra(item)}
            {noticeFor(item.id)}
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" variant={item.connected ? "outline" : "default"} disabled={busy}
                onClick={() => {
                  setNotice(null);
                  setEditing(item);
                }}
                className="rounded-full">
                {item.connected
                  ? <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                  : <Plug className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
                {t(item.connected ? "cashmaxx.integrations.actions.edit" : "cashmaxx.integrations.actions.connect")}
                <span className="sr-only"> {item.label}</span>
              </Button>
              {item.connected ? (
                <>
                  <Button type="button" size="sm" variant="outline" disabled={busy}
                    onClick={() => void runTest(item)} className="rounded-full">
                    {acting === `test:${item.id}`
                      ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                      : <FlaskConical className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
                    {t("cashmaxx.integrations.actions.test")}
                    <span className="sr-only"> {item.label}</span>
                  </Button>
                  <Button type="button" size="sm" variant="ghost" disabled={busy}
                    onClick={() => {
                      setNotice(null);
                      setRemoving(item);
                    }}
                    className="rounded-full text-destructive dark:text-red-400 hover:text-destructive dark:hover:text-red-300">
                    <Unplug className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    {t("cashmaxx.integrations.actions.disconnect")}
                    <span className="sr-only"> {item.label}</span>
                  </Button>
                </>
              ) : null}
              {item.docs_url ? (
                <a href={item.docs_url} target="_blank" rel="noreferrer noopener"
                  className="ml-auto inline-flex items-center gap-1 rounded-full px-2 py-1 text-[12.5px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {t("cashmaxx.integrations.docs")}
                  <span className="sr-only"> {item.label}</span>
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              ) : null}
            </div>
          </IntegrationCard>
        )}
      />
      <ConnectIntegrationDialog
        connection={connection}
        item={editing}
        onClose={() => setEditing(null)}
        onSaved={(item) => {
          setEditing(null);
          setNotice({ scope: item.id, tone: "info", text: t("cashmaxx.integrations.saved", { label: item.label }) });
          void Promise.all([reloadItems().catch(() => undefined), loadSettings()]);
        }}
      />
      <DisconnectIntegrationDialog
        item={removing}
        busy={removing !== null && acting === `remove:${removing.id}`}
        error={removing && notice?.scope === removing.id && notice.tone === "error" ? notice.text : null}
        onCancel={() => setRemoving(null)}
        onConfirm={(item) => void disconnect(item)}
      />
    </div>
  );
}

function Loading() {
  const { t } = useTranslation();
  return (
    <div className="flex h-40 items-center justify-center rounded-panel bg-settings-surface text-sm text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
      {t("settings.status.loading")}
    </div>
  );
}

function IntegrationGroups<T extends IntegrationSummary>({
  items,
  render,
  extra,
}: {
  items: T[];
  render: (item: T) => ReactNode;
  extra?: (category: string) => ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <>
      {groupIntegrations(items).map((group) => {
        const title = t(`cashmaxx.integrations.categories.${group.category}`);
        return (
          <section key={group.category} aria-label={title}>
            <SettingsSectionTitle>{title}</SettingsSectionTitle>
            <div className="grid gap-3">
              {extra?.(group.category)}
              <ul className="grid gap-3">
                {group.items.map((item) => <li key={item.id} className="min-w-0">{render(item)}</li>)}
              </ul>
            </div>
          </section>
        );
      })}
    </>
  );
}

function GroupPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="grid gap-3 rounded-panel bg-settings-surface p-4 sm:p-5">
      <h4 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">{title}</h4>
      {children}
    </div>
  );
}

export function IntegrationStatusBadge({ status }: { status: IntegrationStatus }) {
  const { t } = useTranslation();
  return (
    <span
      className={cn(
        "inline-flex shrink-0 select-none items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12px] font-medium",
        status === "connected" && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        status === "testFailed" && "bg-destructive/10 text-destructive dark:text-red-400",
        status === "notConnected" && "bg-muted text-muted-foreground",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status === "connected" && "bg-emerald-500",
          status === "testFailed" && "bg-destructive",
          status === "notConnected" && "bg-muted-foreground/50",
        )}
      />
      {t(`cashmaxx.integrations.status.${status}`)}
    </span>
  );
}

function IntegrationCard({
  item,
  children,
}: {
  item: IntegrationSummary | IntegrationItem;
  children?: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  const full = "fields" in item ? item : null;
  const Icon = ICONS[item.id] ?? Plug;
  const summary = t(`cashmaxx.integrations.items.${item.id}`, { defaultValue: full?.summary ?? "" });
  const lastTest = full?.last_test ?? null;
  return (
    <article data-testid={`cashmaxx-integration-${item.id}`} aria-label={item.label}
      className="grid gap-4 rounded-panel bg-settings-surface p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Icon className="h-4 w-4" aria-hidden />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <h4 className="min-w-0 break-words text-[14px] font-medium leading-5 text-foreground">{item.label}</h4>
            <IntegrationStatusBadge status={integrationStatus({ ...item, last_test: lastTest })} />
          </div>
          {summary ? <p className="mt-1 break-words text-[12.5px] leading-5 text-muted-foreground">{summary}</p> : null}
          {full ? (
            <p className="mt-1 break-words text-[12px] leading-5 text-muted-foreground">
              {lastTest?.at ? (
                <>
                  {t(lastTest.ok ? "cashmaxx.integrations.lastTestOk" : "cashmaxx.integrations.lastTestFailed")}{" "}
                  <time dateTime={lastTest.at} title={formatDateTime(lastTest.at, i18n.language)} className="tabular-nums">
                    {formatRelativeTime(lastTest.at, i18n.language)}
                  </time>
                  {!lastTest.ok && lastTest.message ? <span className="text-destructive dark:text-red-400"> · {lastTest.message}</span> : null}
                </>
              ) : t("cashmaxx.integrations.neverTested")}
            </p>
          ) : null}
        </div>
      </div>
      {children}
    </article>
  );
}
