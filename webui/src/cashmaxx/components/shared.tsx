import { Check, Copy, PlugZap, RotateCcw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

import { CashmaxxError, isGuardUnavailable, isNotConfigured } from "../api";
import { amountSign, formatSignedUsd } from "../format";

export function CopyButton({ value, label, className }: { value: string; label: string; className?: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(id);
  }, [copied]);
  return (
    <button
      type="button"
      aria-label={copied ? t("cashmaxx.actions.copied") : label}
      title={copied ? t("cashmaxx.actions.copied") : label}
      onClick={() => {
        void copyTextToClipboard(value).then((ok) => setCopied(ok));
      }}
      className={cn(
        "touch-target inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors settings-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      {copied ? <Check className="h-3.5 w-3.5" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
    </button>
  );
}

export function CashmaxxCard({
  title,
  action,
  children,
  className,
  labelledBy,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  labelledBy?: string;
}) {
  return (
    <section aria-labelledby={labelledBy} className={cn("rounded-panel bg-settings-surface p-4 sm:p-5", className)}>
      {title || action ? (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          {title ? (
            <h3 id={labelledBy} className="select-none text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
              {title}
            </h3>
          ) : <span />}
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function NetworkBadge({ network }: { network: string }) {
  const { t } = useTranslation();
  if (network === "base-sepolia") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2.5 py-1 text-[12px] font-semibold text-amber-800 ring-1 ring-inset ring-amber-500/30 dark:text-amber-200">
        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
        {t("cashmaxx.network.sepolia")}
      </span>
    );
  }
  if (network === "base") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-blue-500/10 px-2.5 py-1 text-[12px] font-medium text-blue-700 dark:text-blue-300">
        <span className="h-1.5 w-1.5 rounded-full bg-blue-500" aria-hidden />
        {t("cashmaxx.network.mainnet")}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
      {network === "fake" ? t("cashmaxx.network.fake") : network || t("cashmaxx.network.unknown")}
    </span>
  );
}

/** A signed amount: coloured by sign, with the sign in the text and a screen-reader word. */
export function SignedAmount({
  value,
  className,
  invert = false,
}: {
  value: string;
  className?: string;
  /** Show a positive value as a negative (costs are stored as positive numbers). */
  invert?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const signed = invert ? negate(value) : value;
  const sign = amountSign(signed);
  return (
    <span
      className={cn(
        "tabular-nums",
        sign === "positive" && "text-emerald-700 dark:text-emerald-400",
        sign === "negative" && "text-destructive dark:text-red-400",
        sign === "zero" && "text-muted-foreground",
        className,
      )}
    >
      {formatSignedUsd(signed, i18n.language)}
      {sign !== "zero" ? (
        <span className="sr-only"> ({t(sign === "positive" ? "cashmaxx.pnl.gain" : "cashmaxx.pnl.loss")})</span>
      ) : null}
    </span>
  );
}

function negate(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith("-")) return trimmed.slice(1);
  return Number(trimmed) === 0 ? trimmed : `-${trimmed}`;
}

export function GuardUnavailable({ onRetry, notConfigured = false }: { onRetry?: () => void; notConfigured?: boolean }) {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      className="flex flex-col items-center gap-3 rounded-panel bg-settings-surface px-6 py-10 text-center"
    >
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <PlugZap className="h-5 w-5" aria-hidden />
      </div>
      <p className="text-[15px] font-medium text-foreground">
        {t(notConfigured ? "cashmaxx.guard.notConfiguredTitle" : "cashmaxx.guard.unavailableTitle")}
      </p>
      <p className="max-w-sm text-[13px] leading-5 text-muted-foreground">
        {t(notConfigured ? "cashmaxx.guard.notConfiguredBody" : "cashmaxx.guard.unavailableBody")}{" "}
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12px] text-foreground">
          {notConfigured ? "cashmaxx onboard" : "cashmaxx guard"}
        </code>
      </p>
      {onRetry ? (
        <Button type="button" size="sm" variant="outline" onClick={onRetry} className="mt-1 rounded-full">
          <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          {t("cashmaxx.actions.retry")}
        </Button>
      ) : null}
    </div>
  );
}

export function errorMessage(error: unknown, t: (key: string) => string): string {
  if (error instanceof CashmaxxError) {
    if (error.status === 409) return `${t("cashmaxx.errors.frozen")} ${error.message}`.trim();
    if (error.status === 422) return `${t("cashmaxx.errors.invalid")} ${error.message}`.trim();
    if (error.status === 401 || error.status === 403) return t("cashmaxx.errors.owner");
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** Either the guard-down empty state, or an inline error with a retry. */
export function CashmaxxErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useTranslation();
  if (isGuardUnavailable(error)) return <GuardUnavailable onRetry={onRetry} notConfigured={isNotConfigured(error)} />;
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-control border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive dark:text-red-400">
      <span className="min-w-0">{errorMessage(error, t)}</span>
      {onRetry ? (
        <Button type="button" size="sm" variant="ghost" onClick={onRetry} className="h-8 rounded-full text-destructive dark:text-red-400">
          {t("cashmaxx.actions.retry")}
        </Button>
      ) : null}
    </div>
  );
}

export function InlineNotice({ tone, children }: { tone: "error" | "info"; children: ReactNode }) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "rounded-control border px-4 py-2.5 text-[13px]",
        tone === "error"
          ? "border-destructive/20 bg-destructive/5 text-destructive dark:text-red-400"
          : "border-border/55 bg-muted/35 text-muted-foreground",
      )}
    >
      {children}
    </div>
  );
}
