import { Loader2, Plus, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  ReadOnlyRow,
  RestartSettingsFooter,
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/shared/SettingsControls";
import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { fetchCashmaxxSettings, patchCashmaxxSettings, type CashmaxxConnection } from "../api";
import { isAllowlistEntry } from "../format";
import { useCashmaxxQuery } from "../hooks";
import { isPromptCancelled, useOwnerSession } from "../owner-session";
import {
  diffSettings,
  formFromSettings,
  validateSettingsForm,
  type CashmaxxSettingsForm as FormState,
  type SettingsFieldKey,
} from "../settings-form";
import {
  ALL_EARNING_METHODS,
  ALL_RULES,
  COMPUTE_MODES,
  type CashmaxxSettings,
  type EarningMethod,
  type SafetyRule,
} from "../types";
import { CashmaxxErrorState, errorMessage, InlineNotice, NetworkBadge } from "./shared";

export function CashmaxxSettingsForm({ connection }: { connection: CashmaxxConnection }) {
  const { token } = connection;
  const { t } = useTranslation();
  const query = useCashmaxxQuery(() => fetchCashmaxxSettings(token), `settings:${token}`);

  if (!query.data) {
    if (query.error) return <CashmaxxErrorState error={query.error} onRetry={() => void query.reload()} />;
    return (
      <div className="flex h-40 items-center justify-center rounded-panel bg-settings-surface text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
        {t("settings.status.loading")}
      </div>
    );
  }
  return <SettingsEditor connection={connection} initial={query.data} />;
}

function SettingsEditor({ connection, initial }: { connection: CashmaxxConnection; initial: CashmaxxSettings }) {
  const { t } = useTranslation();
  const { runAsOwner } = useOwnerSession();
  const [original, setOriginal] = useState(initial);
  const [form, setForm] = useState<FormState>(() => formFromSettings(initial));
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ error: boolean; text: string } | null>(null);

  const patch = useMemo(() => diffSettings(original, form), [original, form]);
  const dirty = Object.keys(patch).length > 0;
  const { errors, warnings } = useMemo(() => validateSettingsForm(form), [form]);
  const hasErrors = Object.keys(errors).length > 0;

  const set = <K extends SettingsFieldKey>(key: K, value: FormState[K]) => {
    setStatus(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };
  const toggleIn = <T extends string>(list: T[], value: T, on: boolean): T[] =>
    on ? (list.includes(value) ? list : [...list, value]) : list.filter((item) => item !== value);

  const save = async () => {
    if (!dirty || hasErrors) return;
    setSaving(true);
    setStatus(null);
    try {
      const updated = await runAsOwner((session) => patchCashmaxxSettings(connection, patch, session));
      const next = updated ?? await fetchCashmaxxSettings(connection.token);
      setOriginal(next);
      setForm(formFromSettings(next));
      setStatus({ error: false, text: t("cashmaxx.settings.saved") });
    } catch (err) {
      if (!isPromptCancelled(err)) setStatus({ error: true, text: errorMessage(err, t) });
    } finally {
      setSaving(false);
    }
  };

  const fieldError = (key: SettingsFieldKey) => (errors[key] ? t(errors[key] as string) : undefined);

  return (
    <div className="settings-stack">
      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.budget")}</SettingsSectionTitle>
        <SettingsGroup>
          <DecimalRow id="budgetUsd" title={t("cashmaxx.settings.fields.budgetUsd")}
            hint={t("cashmaxx.settings.hints.budgetUsd")}
            value={form.budgetUsd} error={fieldError("budgetUsd")} onChange={(v) => set("budgetUsd", v)} />
          <DecimalRow id="perTxApprovalUsd" title={t("cashmaxx.settings.fields.perTxApprovalUsd")}
            hint={t("cashmaxx.settings.hints.perTxApprovalUsd")}
            value={form.perTxApprovalUsd} error={fieldError("perTxApprovalUsd")}
            onChange={(v) => set("perTxApprovalUsd", v)} />
          <DecimalRow id="dailyCapUsd" title={t("cashmaxx.settings.fields.dailyCapUsd")}
            hint={t("cashmaxx.settings.hints.dailyCapUsd")}
            value={form.dailyCapUsd} error={fieldError("dailyCapUsd")} onChange={(v) => set("dailyCapUsd", v)} />
          <DecimalRow id="maxPaymentsPerHour" title={t("cashmaxx.settings.fields.maxPaymentsPerHour")}
            hint={t("cashmaxx.settings.hints.maxPaymentsPerHour")} integer
            value={form.maxPaymentsPerHour} error={fieldError("maxPaymentsPerHour")}
            onChange={(v) => set("maxPaymentsPerHour", v)} />
        </SettingsGroup>
        {warnings.length ? (
          <div className="mt-3 space-y-1" role="status">
            {warnings.map((key) => (
              <p key={key} className="settings-list-inset text-[12.5px] leading-5 text-amber-700 dark:text-amber-300">
                {t(key)}
              </p>
            ))}
          </div>
        ) : null}
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.approvals")}</SettingsSectionTitle>
        <SettingsGroup>
          <SwitchRow title={t("cashmaxx.settings.fields.newRecipientNeedsApproval")}
            description={t("cashmaxx.settings.hints.newRecipientNeedsApproval")}
            checked={form.newRecipientNeedsApproval}
            onChange={(on) => set("newRecipientNeedsApproval", on)} />
          <AllowlistEditor entries={form.allowlist} error={fieldError("allowlist")}
            onChange={(entries) => set("allowlist", entries)} />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.rules")}</SettingsSectionTitle>
        <SettingsGroup>
          {ALL_RULES.map((rule: SafetyRule) => (
            <SwitchRow key={rule} title={t(`cashmaxx.settings.rules.${rule}.title`)}
              description={t(`cashmaxx.settings.rules.${rule}.description`)}
              checked={form.rules.includes(rule)}
              onChange={(on) => set("rules", toggleIn(form.rules, rule, on))} />
          ))}
          {form.rules.includes("loss_stop") ? (
            <DecimalRow id="lossStopUsd" title={t("cashmaxx.settings.fields.lossStopUsd")}
              hint={t("cashmaxx.settings.hints.lossStopUsd")}
              value={form.lossStopUsd} error={fieldError("lossStopUsd")} onChange={(v) => set("lossStopUsd", v)} />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.earning")}</SettingsSectionTitle>
        <SettingsGroup>
          {ALL_EARNING_METHODS.map((method: EarningMethod) => (
            <SwitchRow key={method} title={t(`cashmaxx.settings.earning.${method}.title`)}
              description={t(`cashmaxx.settings.earning.${method}.description`)}
              checked={form.earningMethods.includes(method)}
              onChange={(on) => set("earningMethods", toggleIn(form.earningMethods, method, on))} />
          ))}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.compute")}</SettingsSectionTitle>
        <SettingsGroup>
          <div className="settings-list-inset pb-1 pt-4">
            <p className="text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.settings.compute.explainer")}</p>
          </div>
          <fieldset className="settings-list-inset grid gap-2 py-3">
            <legend className="sr-only">{t("cashmaxx.settings.fields.computePaymentMode")}</legend>
            {COMPUTE_MODES.map((mode) => {
              const checked = form.computePaymentMode === mode;
              return (
                <label key={mode}
                  className={cn(
                    "flex cursor-pointer items-start gap-3 rounded-control border px-3.5 py-3 transition-colors",
                    "focus-within:ring-2 focus-within:ring-ring",
                    checked ? "border-foreground/40 bg-background" : "border-border/50 settings-hover",
                  )}
                >
                  <input type="radio" name="cashmaxx-compute-mode" value={mode} checked={checked}
                    onChange={() => set("computePaymentMode", mode)}
                    className="mt-1 h-4 w-4 shrink-0 accent-foreground" />
                  <span className="min-w-0">
                    <span className="block text-[14px] font-medium text-foreground">
                      {t(`cashmaxx.settings.compute.modes.${mode}.title`)}
                    </span>
                    <span className="mt-0.5 block text-[12.5px] leading-5 text-muted-foreground">
                      {t(`cashmaxx.settings.compute.modes.${mode}.description`)}
                    </span>
                  </span>
                </label>
              );
            })}
          </fieldset>
          {form.computePaymentMode === "reimburse" ? (
            <>
              <TextRow id="ownerWallet" title={t("cashmaxx.settings.fields.ownerWallet")}
                hint={t("cashmaxx.settings.hints.ownerWallet")} placeholder="0x…" mono
                value={form.ownerWallet} error={fieldError("ownerWallet")} onChange={(v) => set("ownerWallet", v)} />
              <DecimalRow id="reimburseIntervalHours" title={t("cashmaxx.settings.fields.reimburseIntervalHours")}
                hint={t("cashmaxx.settings.hints.reimburseIntervalHours")} integer suffix={t("cashmaxx.settings.hoursSuffix")}
                value={form.reimburseIntervalHours} error={fieldError("reimburseIntervalHours")}
                onChange={(v) => set("reimburseIntervalHours", v)} />
            </>
          ) : null}
          {form.computePaymentMode === "owner_topup" ? (
            <DecimalRow id="ownerTopupThresholdUsd" title={t("cashmaxx.settings.fields.ownerTopupThresholdUsd")}
              hint={t("cashmaxx.settings.hints.ownerTopupThresholdUsd")}
              value={form.ownerTopupThresholdUsd} error={fieldError("ownerTopupThresholdUsd")}
              onChange={(v) => set("ownerTopupThresholdUsd", v)} />
          ) : null}
          {form.computePaymentMode === "x402_gateway" ? (
            <TextRow id="x402GatewayUrl" title={t("cashmaxx.settings.fields.x402GatewayUrl")}
              hint={t("cashmaxx.settings.hints.x402GatewayUrl")} placeholder="https://"
              value={form.x402GatewayUrl} error={fieldError("x402GatewayUrl")}
              onChange={(v) => set("x402GatewayUrl", v)} />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.public")}</SettingsSectionTitle>
        <SettingsGroup>
          <SwitchRow title={t("cashmaxx.settings.fields.publicPnl")}
            description={t("cashmaxx.settings.hints.publicPnl")}
            checked={form.publicPnl} onChange={(on) => set("publicPnl", on)} />
          {form.publicPnl ? (
            <ReadOnlyRow title={t("cashmaxx.settings.fields.publicUrl")}
              value={original.publicPnlUrl || t("cashmaxx.settings.publicUrlFallback")} />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("cashmaxx.settings.groups.network")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow title={t("cashmaxx.settings.fields.network")}>
            <div className="flex flex-col items-start gap-1 sm:items-end">
              <NetworkBadge network={String(original.network)} />
              <span className="text-[12px] text-muted-foreground">{t("cashmaxx.settings.networkReadOnly")}</span>
            </div>
          </SettingsRow>
        </SettingsGroup>
      </section>

      <div className="sticky bottom-0 z-10 rounded-panel bg-settings-surface shadow-[0_-1px_0_hsl(var(--border)/0.5)]">
        {status?.error ? <div className="px-4 pt-3"><InlineNotice tone="error">{status.text}</InlineNotice></div> : null}
        <RestartSettingsFooter
          dirty={dirty}
          saving={saving}
          pendingRestart={false}
          disabled={hasErrors}
          error={hasErrors}
          message={hasErrors ? t("cashmaxx.settings.fixErrors") : status && !status.error && !dirty ? status.text : undefined}
          dirtyMessage={t("cashmaxx.settings.unsaved")}
          saveLabel={t("cashmaxx.settings.save")}
          onSave={() => void save()}
          onReset={() => {
            setStatus(null);
            setForm(formFromSettings(original));
          }}
        />
      </div>
    </div>
  );
}

function FieldShell({ id, error, children }: { id: string; error?: string; children: ReactNode }) {
  return (
    <div className="flex w-full flex-col items-stretch gap-1 sm:items-end">
      {children}
      {error ? (
        <p id={`cashmaxx-${id}-error`} className="text-[12px] text-destructive dark:text-red-400 sm:text-right">{error}</p>
      ) : null}
    </div>
  );
}

function DecimalRow({
  id,
  title,
  hint,
  value,
  error,
  integer = false,
  suffix,
  onChange,
}: {
  id: string;
  title: string;
  hint?: string;
  value: string;
  error?: string;
  integer?: boolean;
  suffix?: string;
  onChange: (value: string) => void;
}) {
  const unit = suffix ?? (integer ? undefined : "USD");
  return (
    <SettingsRow title={title} description={hint}>
      <FieldShell id={id} error={error}>
        <div className="relative w-full sm:max-w-[200px]">
          <Input
            id={`cashmaxx-${id}`}
            aria-label={title}
            inputMode={integer ? "numeric" : "decimal"}
            autoComplete="off"
            value={value}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `cashmaxx-${id}-error` : undefined}
            onChange={(event) => onChange(event.target.value)}
            className={cn("h-9 w-full rounded-full text-[13px] tabular-nums", unit && "pr-12",
              error && "border-destructive/60")}
          />
          {unit ? (
            <span className="pointer-events-none absolute inset-y-0 right-3 flex select-none items-center text-[12px] text-muted-foreground">
              {unit}
            </span>
          ) : null}
        </div>
      </FieldShell>
    </SettingsRow>
  );
}

function TextRow({
  id,
  title,
  hint,
  value,
  error,
  placeholder,
  mono = false,
  onChange,
}: {
  id: string;
  title: string;
  hint?: string;
  value: string;
  error?: string;
  placeholder?: string;
  mono?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <SettingsRow title={title} description={hint}>
      <FieldShell id={id} error={error}>
        <Input
          id={`cashmaxx-${id}`}
          aria-label={title}
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `cashmaxx-${id}-error` : undefined}
          onChange={(event) => onChange(event.target.value)}
          className={cn("h-9 w-full rounded-full text-[13px] sm:w-[320px]", mono && "font-mono",
            error && "border-destructive/60")}
        />
      </FieldShell>
    </SettingsRow>
  );
}

function SwitchRow({
  title,
  description,
  checked,
  onChange,
}: {
  title: string;
  description: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="settings-row rounded-xl transition-colors settings-hover focus-within:bg-sidebar-accent/60">
      <div className="min-w-0">
        <p className="select-none text-[14px] font-medium leading-5 text-foreground">{title}</p>
        <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">{description}</p>
      </div>
      <div className="settings-control">
        <ToggleButton checked={checked} onChange={onChange} ariaLabel={title}
          label={checked ? t("settings.values.on") : t("settings.values.off")} />
      </div>
    </div>
  );
}

function AllowlistEditor({
  entries,
  error,
  onChange,
}: {
  entries: string[];
  error?: string;
  onChange: (entries: string[]) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  useEffect(() => setDraftError(null), [draft]);

  const add = () => {
    const value = draft.trim();
    if (!value) return;
    if (!isAllowlistEntry(value)) {
      setDraftError(t("cashmaxx.validation.allowlist"));
      return;
    }
    if (entries.some((entry) => entry.toLowerCase() === value.toLowerCase())) {
      setDraftError(t("cashmaxx.settings.allowlist.duplicate"));
      return;
    }
    onChange([...entries, value]);
    setDraft("");
  };

  return (
    <div className="settings-list-inset py-3">
      <p className="text-[14px] font-medium leading-5 text-foreground">{t("cashmaxx.settings.fields.allowlist")}</p>
      <p className="mt-0.5 text-[12.5px] leading-5 text-muted-foreground">{t("cashmaxx.settings.hints.allowlist")}</p>
      {entries.length ? (
        <ul aria-label={t("cashmaxx.settings.fields.allowlist")} className="mt-3 flex flex-wrap gap-2">
          {entries.map((entry) => (
            <li key={entry}
              className={cn("inline-flex max-w-full items-center gap-1 rounded-full bg-background py-1 pl-3 pr-1 ring-1 ring-inset",
                isAllowlistEntry(entry) ? "ring-border/50" : "ring-destructive/50")}>
              <code className="min-w-0 truncate font-mono text-[12px] text-foreground" title={entry}>{entry}</code>
              <button type="button" aria-label={t("cashmaxx.settings.allowlist.remove", { entry })}
                onClick={() => onChange(entries.filter((item) => item !== entry))}
                className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors settings-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <X className="h-3 w-3" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-[12.5px] text-muted-foreground">{t("cashmaxx.settings.allowlist.empty")}</p>
      )}
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <Input
          aria-label={t("cashmaxx.settings.allowlist.addLabel")}
          placeholder={t("cashmaxx.settings.allowlist.placeholder")}
          autoComplete="off"
          spellCheck={false}
          value={draft}
          aria-invalid={draftError ? true : undefined}
          aria-describedby={draftError ? "cashmaxx-allowlist-draft-error" : undefined}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
          className="h-9 min-w-0 flex-1 rounded-full font-mono text-[13px]"
        />
        <Button type="button" size="sm" variant="outline" onClick={add} disabled={!draft.trim()}
          className="rounded-full">
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          {t("cashmaxx.settings.allowlist.add")}
        </Button>
      </div>
      {draftError || error ? (
        <p id="cashmaxx-allowlist-draft-error" role="alert" className="mt-2 text-[12px] text-destructive dark:text-red-400">
          {draftError ?? error}
        </p>
      ) : null}
    </div>
  );
}
