import QRCode from "qrcode";
import { Loader2, OctagonX, ShieldAlert, Snowflake } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";

import {
  fetchEvents,
  fetchHealth,
  fetchLedger,
  fetchWallet,
  freezeGuard,
  isGuardUnavailable,
  unfreezeGuard,
  type CashmaxxConnection,
} from "../api";
import { formatDateTime, formatRelativeTime, formatUsd } from "../format";
import { useCashmaxxQuery } from "../hooks";
import { isPromptCancelled, useOwnerSession } from "../owner-session";
import type { PnlWindow } from "../types";
import {
  CashmaxxCard,
  CashmaxxErrorState,
  CopyButton,
  errorMessage,
  InlineNotice,
  NetworkBadge,
  SignedAmount,
} from "./shared";

const RECENT_EVENTS = 12;

export function CashmaxxOverview({ connection }: { connection: CashmaxxConnection }) {
  const { token } = connection;
  const status = useCashmaxxQuery(
    async () => {
      const [health, wallet] = await Promise.all([fetchHealth(token), fetchWallet(token)]);
      return { health, wallet };
    },
    `status:${token}`,
  );

  if (!status.data) {
    if (status.error) return <CashmaxxErrorState error={status.error} onRetry={() => void status.reload()} />;
    return <LoadingBlock />;
  }
  if (status.error && isGuardUnavailable(status.error)) {
    return <CashmaxxErrorState error={status.error} onRetry={() => void status.reload()} />;
  }

  const { health, wallet } = status.data;
  const network = wallet.network || health.network;
  return (
    <div className="settings-stack">
      <FreezeControls connection={connection} frozen={health.frozen} onChanged={() => void status.reload()} />
      <WalletCard address={wallet.address} network={network}
        balance={wallet.balance_usdc} available={wallet.available_budget_usd} />
      <PnlCard token={token} />
      <EventsCard token={token} />
    </div>
  );
}

function LoadingBlock() {
  const { t } = useTranslation();
  return (
    <div className="flex h-40 items-center justify-center rounded-panel bg-settings-surface text-sm text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
      {t("settings.status.loading")}
    </div>
  );
}

function WalletCard({
  address,
  network,
  balance,
  available,
}: {
  address: string;
  network: string;
  balance: string;
  available: string;
}) {
  const { t, i18n } = useTranslation();
  const [qr, setQr] = useState("");
  useEffect(() => {
    if (!address) return;
    let cancelled = false;
    void QRCode.toDataURL(address, {
      width: 152,
      margin: 1,
      color: { dark: "#111827", light: "#ffffff" },
    })
      .then((url) => {
        if (!cancelled) setQr(url);
      })
      .catch(() => {
        if (!cancelled) setQr("");
      });
    return () => {
      cancelled = true;
    };
  }, [address]);

  return (
    <CashmaxxCard title={t("cashmaxx.wallet.title")} labelledBy="cashmaxx-wallet-title"
      action={<NetworkBadge network={network} />}>
      {network === "base-sepolia" ? (
        <p className="mb-3 text-[12.5px] leading-5 text-amber-800 dark:text-amber-200">
          {t("cashmaxx.network.sepoliaNote")}
        </p>
      ) : null}
      <div className="flex flex-col gap-5 sm:flex-row sm:items-start">
        <div className="min-w-0 flex-1 space-y-4">
          <div>
            <p className="text-[12px] font-medium text-muted-foreground">{t("cashmaxx.wallet.address")}</p>
            <div className="mt-1 flex items-start gap-1">
              <code data-testid="cashmaxx-wallet-address" className="min-w-0 break-all font-mono text-[13px] leading-6 text-foreground">
                {address || "—"}
              </code>
              {address ? <CopyButton value={address} label={t("cashmaxx.actions.copyAddress")} /> : null}
            </div>
            <p className="mt-1 text-[12px] leading-5 text-muted-foreground">{t("cashmaxx.wallet.fundHint")}</p>
          </div>
          <dl className="grid grid-cols-2 gap-3">
            <div className="rounded-control bg-background/60 px-3 py-2.5 ring-1 ring-inset ring-border/40">
              <dt className="text-[12px] text-muted-foreground">{t("cashmaxx.wallet.balance")}</dt>
              <dd className="mt-0.5 text-[20px] font-semibold tabular-nums tracking-[-0.01em] text-foreground">
                {formatUsd(balance, i18n.language)}
                <span className="ml-1 text-[12px] font-medium text-muted-foreground">USDC</span>
              </dd>
            </div>
            <div className="rounded-control bg-background/60 px-3 py-2.5 ring-1 ring-inset ring-border/40">
              <dt className="text-[12px] text-muted-foreground">{t("cashmaxx.wallet.available")}</dt>
              <dd className="mt-0.5 text-[20px] font-semibold tabular-nums tracking-[-0.01em] text-foreground">
                {formatUsd(available, i18n.language)}
              </dd>
            </div>
          </dl>
        </div>
        {address ? (
          <div className="grid h-[164px] w-[164px] shrink-0 place-items-center self-center rounded-control bg-white shadow-[inset_0_0_0_1px_oklch(0_0_0/0.1)] sm:self-start">
            {qr ? (
              <img src={qr} alt={t("cashmaxx.wallet.qrAlt")} className="h-[152px] w-[152px]" />
            ) : (
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-hidden />
            )}
          </div>
        ) : null}
      </div>
    </CashmaxxCard>
  );
}

function PnlCard({ token }: { token: string }) {
  const { t } = useTranslation();
  const [pnlWindow, setPnlWindow] = useState<PnlWindow>("7d");
  const ledger = useCashmaxxQuery(() => fetchLedger(token, pnlWindow), `ledger:${token}:${pnlWindow}`);
  const data = ledger.data;
  const windows: PnlWindow[] = ["7d", "30d", "all"];

  return (
    <CashmaxxCard
      title={t("cashmaxx.pnl.title")}
      labelledBy="cashmaxx-pnl-title"
      action={(
        <SegmentedControl
          value={pnlWindow}
          ariaLabel={t("cashmaxx.pnl.windowLabel")}
          options={windows.map((value) => ({ value, label: t(`cashmaxx.pnl.windows.${value}`) }))}
          onChange={setPnlWindow}
        />
      )}
    >
      {ledger.error && !data ? (
        <CashmaxxErrorState error={ledger.error} onRetry={() => void ledger.reload()} />
      ) : !data ? (
        <div className="flex h-24 items-center justify-center text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        </div>
      ) : (
        <div aria-busy={ledger.loading} className={ledger.loading ? "opacity-70 transition-opacity" : undefined}>
          <dl className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-3">
            <PnlStat label={t("cashmaxx.pnl.income")}><SignedAmount value={data.income} /></PnlStat>
            <PnlStat label={t("cashmaxx.pnl.costs")}><SignedAmount value={data.costs} invert /></PnlStat>
            <PnlStat label={t("cashmaxx.pnl.net")} strong><SignedAmount value={data.net} /></PnlStat>
          </dl>
          <div className="mt-4 overflow-x-auto">
            {data.by_category.length ? (
              <table className="w-full min-w-[280px] text-left text-[13px]">
                <caption className="sr-only">{t("cashmaxx.pnl.byCategory")}</caption>
                <thead>
                  <tr className="border-b border-border/50 text-[12px] text-muted-foreground">
                    <th scope="col" className="py-2 pr-3 font-medium">{t("cashmaxx.pnl.category")}</th>
                    <th scope="col" className="py-2 pr-3 font-medium">{t("cashmaxx.pnl.type")}</th>
                    <th scope="col" className="py-2 text-right font-medium">{t("cashmaxx.pnl.amount")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_category.map((row) => (
                    <tr key={`${row.direction}:${row.category}`} className="border-b border-border/30 last:border-0">
                      <td className="py-2 pr-3 text-foreground">
                        {t(`cashmaxx.categories.${row.category}`, { defaultValue: row.category })}
                      </td>
                      <td className="py-2 pr-3 text-muted-foreground">
                        {t(row.direction === "income" ? "cashmaxx.pnl.income" : "cashmaxx.pnl.cost")}
                      </td>
                      <td className="py-2 text-right">
                        <SignedAmount value={row.amount} invert={row.direction === "cost" && !row.amount.startsWith("-")} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-[13px] text-muted-foreground">{t("cashmaxx.pnl.empty")}</p>
            )}
          </div>
        </div>
      )}
    </CashmaxxCard>
  );
}

function PnlStat({ label, strong, children }: { label: string; strong?: boolean; children: ReactNode }) {
  return (
    <div className="rounded-control bg-background/60 px-3 py-2.5 ring-1 ring-inset ring-border/40">
      <dt className="text-[12px] text-muted-foreground">{label}</dt>
      <dd className={strong ? "mt-0.5 text-[20px] font-semibold tracking-[-0.01em]" : "mt-0.5 text-[17px] font-medium"}>
        {children}
      </dd>
    </div>
  );
}

function EventsCard({ token }: { token: string }) {
  const { t, i18n } = useTranslation();
  const events = useCashmaxxQuery(() => fetchEvents(token), `events:${token}`);
  const rows = (events.data ?? []).slice(-RECENT_EVENTS).reverse();
  return (
    <CashmaxxCard title={t("cashmaxx.events.title")} labelledBy="cashmaxx-events-title">
      {events.error && !events.data ? (
        <CashmaxxErrorState error={events.error} onRetry={() => void events.reload()} />
      ) : !events.data ? (
        <div className="flex h-16 items-center justify-center text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        </div>
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("cashmaxx.events.empty")}</p>
      ) : (
        <ul className="divide-y divide-border/35">
          {rows.map((event) => (
            <li key={event.id} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 py-2">
              <div className="min-w-0">
                <span className="text-[13px] font-medium text-foreground">
                  {t(`cashmaxx.events.types.${event.type}`, { defaultValue: humanize(event.type) })}
                </span>
                {event.actor ? (
                  <span className="ml-2 rounded-full bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {t(`cashmaxx.events.actors.${event.actor}`, { defaultValue: event.actor })}
                  </span>
                ) : null}
                {event.summary ? (
                  <p className="mt-0.5 break-words text-[12px] text-muted-foreground">{event.summary}</p>
                ) : null}
              </div>
              {event.ts ? (
                <time dateTime={event.ts} title={formatDateTime(event.ts, i18n.language)}
                  className="shrink-0 text-[12px] tabular-nums text-muted-foreground">
                  {formatRelativeTime(event.ts, i18n.language)}
                </time>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </CashmaxxCard>
  );
}

function humanize(value: string): string {
  const text = value.replace(/[_.]/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : value;
}

function FreezeControls({
  connection,
  frozen,
  onChanged,
}: {
  connection: CashmaxxConnection;
  frozen: boolean;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { runAsOwner } = useOwnerSession();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const freeze = async () => {
    setBusy(true);
    setError(null);
    try {
      await freezeGuard(connection, reason.trim() || t("cashmaxx.freeze.defaultReason"));
      setConfirmOpen(false);
      setReason("");
      onChanged();
    } catch (err) {
      setError(errorMessage(err, t));
    } finally {
      setBusy(false);
    }
  };

  const unfreeze = async () => {
    setBusy(true);
    setError(null);
    try {
      await runAsOwner((session) => unfreezeGuard(connection, session));
      onChanged();
    } catch (err) {
      if (!isPromptCancelled(err)) setError(errorMessage(err, t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {frozen ? (
        <div role="status" className="flex flex-col gap-3 rounded-panel border border-sky-500/30 bg-sky-500/10 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <Snowflake className="mt-0.5 h-4 w-4 shrink-0 text-sky-700 dark:text-sky-300" aria-hidden />
            <div>
              <p className="text-[14px] font-semibold text-foreground">{t("cashmaxx.freeze.frozenTitle")}</p>
              <p className="text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.freeze.frozenBody")}</p>
            </div>
          </div>
          <Button type="button" size="sm" variant="outline" disabled={busy}
            onClick={() => void unfreeze()} className="shrink-0 rounded-full">
            {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {t("cashmaxx.freeze.unfreeze")}
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3 rounded-panel bg-settings-surface px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            <div>
              <p className="text-[14px] font-medium text-foreground">{t("cashmaxx.freeze.killSwitch")}</p>
              <p className="text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.freeze.killSwitchBody")}</p>
            </div>
          </div>
          <Button type="button" size="sm" variant="destructive" onClick={() => setConfirmOpen(true)}
            className="shrink-0 rounded-full">
            <OctagonX className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t("cashmaxx.freeze.freeze")}
          </Button>
        </div>
      )}
      {error ? <InlineNotice tone="error">{error}</InlineNotice> : null}
      <Dialog open={confirmOpen} onOpenChange={(open) => { if (!busy) setConfirmOpen(open); }}>
        <DialogContent className="w-[min(calc(100vw-2rem),26rem)] gap-0 p-5">
          <DialogHeader className="space-y-0">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {t("cashmaxx.freeze.confirmTitle")}
            </DialogTitle>
            <DialogDescription className="mt-2 text-[13px] leading-5">
              {t("cashmaxx.freeze.confirmBody")}
            </DialogDescription>
          </DialogHeader>
          <label htmlFor="cashmaxx-freeze-reason" className="mt-4 block text-[12.5px] font-medium text-foreground">
            {t("cashmaxx.freeze.reasonLabel")}
          </label>
          <Input id="cashmaxx-freeze-reason" value={reason} maxLength={200}
            placeholder={t("cashmaxx.freeze.defaultReason")}
            onChange={(event) => setReason(event.target.value)} className="mt-1.5 h-10 text-[14px]" />
          <DialogFooter className="mt-6 gap-2">
            <Button type="button" variant="ghost" disabled={busy} onClick={() => setConfirmOpen(false)}
              className="rounded-full">
              {t("cashmaxx.actions.cancel")}
            </Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => void freeze()}
              className="rounded-full">
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              {t("cashmaxx.freeze.confirmAction")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
