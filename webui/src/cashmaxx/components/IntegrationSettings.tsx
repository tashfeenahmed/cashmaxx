// The guard settings that switch an integration's capability on, shown on the Integrations tab
// next to the integration they belong to (email provider and cap, Gmail warm-up, social cap,
// public hosting).

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { parseCap, warmupProgress, WARMUP_WEEKS } from "../integrations-model";
import type { EmailProvider } from "../types";
import { CashmaxxErrorState } from "./shared";

const PROVIDERS: EmailProvider[] = ["none", "gmail", "agentmail"];

export function EmailProviderSwitch({
  value,
  connected,
  disabled,
  onChange,
}: {
  value: EmailProvider;
  connected: Set<string>;
  disabled: boolean;
  onChange: (value: EmailProvider) => void;
}) {
  const { t } = useTranslation();
  const anyConnected = connected.has("gmail") || connected.has("agentmail");
  return (
    <fieldset className="min-w-0">
      <legend className="text-[13.5px] font-medium text-foreground">{t("cashmaxx.integrations.email.provider")}</legend>
      <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">
        {t(anyConnected ? "cashmaxx.integrations.email.providerHint" : "cashmaxx.integrations.email.connectFirst")}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        {PROVIDERS.map((provider) => {
          const checked = value === provider;
          const available = provider === "none" || connected.has(provider);
          return (
            <label key={provider}
              className={cn(
                "inline-flex min-h-9 items-center gap-2 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors",
                "focus-within:ring-2 focus-within:ring-ring",
                checked ? "border-foreground/40 bg-background text-foreground" : "border-border/50 text-muted-foreground",
                available && !disabled ? "cursor-pointer settings-hover" : "cursor-default opacity-60",
              )}
            >
              <input type="radio" name="cashmaxx-email-provider" value={provider} checked={checked}
                disabled={disabled || !available}
                onChange={() => onChange(provider)}
                className="h-3.5 w-3.5 shrink-0 accent-foreground" />
              {t(`cashmaxx.integrations.email.providers.${provider}`)}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

/** A whole-number cap with its own Save button (each save is one owner-scoped settings patch). */
export function CapEditor({
  id,
  label,
  hint,
  value,
  min,
  max,
  disabled,
  onSave,
}: {
  id: string;
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onSave: (value: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const parsed = parseCap(draft, min, max);
  const invalid = parsed === null;
  const changed = !invalid && parsed !== value;
  const inputId = `cashmaxx-${id}`;
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <label htmlFor={inputId} className="text-[13.5px] font-medium text-foreground">{label}</label>
        <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">{hint}</p>
      </div>
      <div className="flex shrink-0 flex-col gap-1 sm:items-end">
        <div className="flex items-center gap-2">
          <div className="relative min-w-0 flex-1 sm:w-[140px] sm:flex-none">
            <Input
              id={inputId}
              inputMode="numeric"
              autoComplete="off"
              value={draft}
              disabled={disabled}
              aria-invalid={invalid ? true : undefined}
              aria-describedby={invalid ? `${inputId}-error` : undefined}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && changed && !disabled) {
                  event.preventDefault();
                  onSave(parsed);
                }
              }}
              className={cn("h-9 w-full rounded-full pr-14 text-[13px] tabular-nums", invalid && "border-destructive/60")}
            />
            <span className="pointer-events-none absolute inset-y-0 right-3 flex select-none items-center text-[12px] text-muted-foreground">
              {t("cashmaxx.integrations.perDay")}
            </span>
          </div>
          <Button type="button" size="sm" variant="outline" disabled={disabled || !changed}
            onClick={() => {
              if (parsed !== null) onSave(parsed);
            }}
            className="shrink-0 rounded-full">
            {t("cashmaxx.integrations.saveSetting")}
            <span className="sr-only"> {label}</span>
          </Button>
        </div>
        {invalid ? (
          <p id={`${inputId}-error`} className="text-[12px] text-destructive">
            {t("cashmaxx.integrations.capRange", { min, max })}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function GmailWarmup({
  connectedAt,
  warmup,
  dailyCap,
  disabled,
  onToggle,
  now,
}: {
  connectedAt: string | null;
  warmup: boolean;
  dailyCap: number;
  disabled: boolean;
  onToggle: (on: boolean) => void;
  now?: number;
}) {
  const { t, i18n } = useTranslation();
  const title = t("cashmaxx.integrations.email.warmup");
  return (
    <div className="grid gap-2 rounded-control bg-background/60 px-3.5 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[13.5px] font-medium text-foreground">{title}</p>
          <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.integrations.email.warmupHint")}</p>
        </div>
        <ToggleButton checked={warmup} disabled={disabled} ariaLabel={title}
          label={warmup ? t("settings.values.on") : t("settings.values.off")} onChange={onToggle} />
      </div>
      <p data-testid="cashmaxx-gmail-warmup" className="text-[12.5px] leading-5 text-foreground">
        {warmupLine(warmupProgress({ connectedAt, warmup, dailyCap, now }), t, i18n.language)}
      </p>
    </div>
  );
}

function warmupLine(
  progress: ReturnType<typeof warmupProgress>,
  t: (key: string, options?: Record<string, unknown>) => string,
  locale: string,
): string {
  switch (progress.state) {
    case "notStarted":
      return t("cashmaxx.integrations.email.warmupNotStarted");
    case "off":
      return t("cashmaxx.integrations.email.warmupOff", { cap: progress.cap });
    case "done":
      return t("cashmaxx.integrations.email.warmupDone", { cap: progress.cap });
    case "ramping":
      return t("cashmaxx.integrations.email.warmupLine", {
        week: progress.week,
        weeks: WARMUP_WEEKS,
        day: progress.day + 1,
        cap: progress.cap,
        next: progress.nextCap,
        date: new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(Date.parse(progress.nextAt)),
      });
  }
}

export function SettingsUnavailable({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useTranslation();
  if (error) return <CashmaxxErrorState error={error} onRetry={onRetry} />;
  return <p className="text-[12.5px] text-muted-foreground">{t("settings.status.loading")}</p>;
}
