import { Check, Inbox, Loader2, MessageCircle, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

import { approveApproval, denyApproval, fetchPendingApprovals, type CashmaxxConnection } from "../api";
import { formatDateTime, formatRelativeTime, formatUsd, shortenAddress } from "../format";
import { useCashmaxxQuery, useVisiblePolling } from "../hooks";
import { isPromptCancelled, useOwnerSession } from "../owner-session";
import type { CashmaxxApproval } from "../types";
import { CashmaxxErrorState, CopyButton, errorMessage, InlineNotice } from "./shared";

export const APPROVALS_POLL_MS = 10_000;

export function CashmaxxApprovals({ connection }: { connection: CashmaxxConnection }) {
  const { token } = connection;
  const { t } = useTranslation();
  const { runAsOwner } = useOwnerSession();
  const approvals = useCashmaxxQuery(() => fetchPendingApprovals(token), `approvals:${token}`);
  const [acting, setActing] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "error" | "info"; text: string } | null>(null);

  useVisiblePolling(() => {
    if (!acting) void approvals.reload();
  }, APPROVALS_POLL_MS);

  const decide = async (approval: CashmaxxApproval, decision: "approve" | "deny") => {
    setActing(`${decision}:${approval.id}`);
    setNotice(null);
    try {
      await runAsOwner((session) =>
        decision === "approve"
          ? approveApproval(connection, approval.id, session)
          : denyApproval(connection, approval.id, session));
      setNotice({
        tone: "info",
        text: t(decision === "approve" ? "cashmaxx.approvals.approved" : "cashmaxx.approvals.denied", {
          amount: formatUsd(approval.amount_usd),
        }),
      });
    } catch (err) {
      if (!isPromptCancelled(err)) setNotice({ tone: "error", text: errorMessage(err, t) });
    } finally {
      setActing(null);
      void approvals.reload();
    }
  };

  const syncNote = (
    <p className="flex items-start gap-2 px-1 text-[12.5px] leading-5 text-muted-foreground">
      <MessageCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      {t("cashmaxx.approvals.telegramNote")}
    </p>
  );

  if (!approvals.data) {
    if (approvals.error) {
      return <CashmaxxErrorState error={approvals.error} onRetry={() => void approvals.reload()} />;
    }
    return (
      <div className="flex h-40 items-center justify-center rounded-panel bg-settings-surface text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
        {t("settings.status.loading")}
      </div>
    );
  }

  const list = approvals.data;
  return (
    <div className="settings-stack">
      {syncNote}
      {notice ? <InlineNotice tone={notice.tone}>{notice.text}</InlineNotice> : null}
      {approvals.error ? <CashmaxxErrorState error={approvals.error} onRetry={() => void approvals.reload()} /> : null}
      {list.length === 0 ? (
        <div role="status" className="flex flex-col items-center gap-2 rounded-panel bg-settings-surface px-6 py-10 text-center">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
            <Inbox className="h-5 w-5" aria-hidden />
          </div>
          <p className="text-[15px] font-medium text-foreground">{t("cashmaxx.approvals.emptyTitle")}</p>
          <p className="max-w-sm text-[13px] leading-5 text-muted-foreground">{t("cashmaxx.approvals.emptyBody")}</p>
        </div>
      ) : (
        <ul aria-label={t("cashmaxx.approvals.listLabel")} className="grid gap-3">
          {list.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} acting={acting}
              onDecide={(decision) => void decide(approval, decision)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ApprovalCard({
  approval,
  acting,
  onDecide,
}: {
  approval: CashmaxxApproval;
  acting: string | null;
  onDecide: (decision: "approve" | "deny") => void;
}) {
  const { t, i18n } = useTranslation();
  const busy = acting !== null;
  const reasonKey = approval.reason === "over_threshold" || approval.reason === "new_recipient"
    ? `cashmaxx.approvals.reasons.${approval.reason}`
    : null;
  const amount = formatUsd(approval.amount_usd, i18n.language);
  return (
    <li
      data-testid={`cashmaxx-approval-${approval.id}`}
      className="rounded-panel bg-settings-surface p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <p className="text-[22px] font-semibold tabular-nums tracking-[-0.01em] text-foreground">
            {amount}
            <span className="ml-1 text-[12px] font-medium text-muted-foreground">USDC</span>
          </p>
          <div className="mt-1 flex items-center gap-1 text-[13px] text-muted-foreground">
            <span>{t("cashmaxx.approvals.to")}</span>
            <code title={approval.to} className="font-mono text-foreground">{shortenAddress(approval.to)}</code>
            {approval.to ? <CopyButton value={approval.to} label={t("cashmaxx.actions.copyRecipient")} className="h-7 w-7" /> : null}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          {approval.reason ? (
            <span className="inline-flex items-center rounded-full bg-amber-500/10 px-2.5 py-1 text-[12px] font-medium text-amber-700 dark:text-amber-300">
              {reasonKey ? t(reasonKey) : approval.reason}
            </span>
          ) : null}
          {approval.ts ? (
            <time dateTime={approval.ts} title={formatDateTime(approval.ts, i18n.language)}
              className="text-[12px] tabular-nums text-muted-foreground">
              {formatRelativeTime(approval.ts, i18n.language)}
            </time>
          ) : null}
        </div>
      </div>
      {approval.purpose ? (
        <p className="mt-3 break-words text-[13.5px] leading-5 text-foreground">
          <span className="sr-only">{t("cashmaxx.approvals.purpose")}: </span>
          {approval.purpose}
        </p>
      ) : null}
      <div className="mt-4 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button type="button" size="sm" variant="outline" disabled={busy}
          onClick={() => onDecide("deny")} className="rounded-full">
          {acting === `deny:${approval.id}`
            ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
            : <X className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
          {t("cashmaxx.approvals.deny")}
        </Button>
        <Button type="button" size="sm" disabled={busy}
          onClick={() => onDecide("approve")} className="rounded-full">
          {acting === `approve:${approval.id}`
            ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
            : <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
          {t("cashmaxx.approvals.approve")}
        </Button>
      </div>
    </li>
  );
}
