import { Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
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
import { formControlFocusClassName } from "@/components/ui/form-control";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { updateIntegration, type CashmaxxConnection } from "../api";
import {
  buildFieldsPayload,
  initialFormValues,
  validateIntegrationForm,
  type IntegrationFormValues,
} from "../integrations-model";
import { isPromptCancelled, useOwnerSession } from "../owner-session";
import type { IntegrationField, IntegrationItem } from "../types";
import { errorMessage, InlineNotice } from "./shared";

/** Connect or edit one integration. Secrets are password inputs and never prefilled. */
export function ConnectIntegrationDialog({
  connection,
  item,
  onClose,
  onSaved,
}: {
  connection: CashmaxxConnection;
  item: IntegrationItem | null;
  onClose: () => void;
  onSaved: (item: IntegrationItem) => void;
}) {
  if (!item) return null;
  return <ConnectForm key={item.id} connection={connection} item={item} onClose={onClose} onSaved={onSaved} />;
}

function ConnectForm({
  connection,
  item,
  onClose,
  onSaved,
}: {
  connection: CashmaxxConnection;
  item: IntegrationItem;
  onClose: () => void;
  onSaved: (item: IntegrationItem) => void;
}) {
  const { t } = useTranslation();
  const { runAsOwner } = useOwnerSession();
  const [values, setValues] = useState<IntegrationFormValues>(() => initialFormValues(item.fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);


  const setValue = (name: string, value: string) => {
    const next = { ...values, [name]: value };
    setValues(next);
    if (submitted) setErrors(validateIntegrationForm(item.fields, next));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setSubmitted(true);
    const found = validateIntegrationForm(item.fields, values);
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    setError(null);
    try {
      const fields = buildFieldsPayload(item.fields, values);
      const updated = await runAsOwner((session) => updateIntegration(connection, item.id, fields, session));
      onSaved(updated ?? item);
    } catch (err) {
      if (!isPromptCancelled(err)) setError(errorMessage(err, t));
    } finally {
      setBusy(false);
    }
  };

  const title = t(item.connected ? "cashmaxx.integrations.dialog.editTitle" : "cashmaxx.integrations.dialog.connectTitle", {
    label: item.label,
  });

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] w-[min(calc(100vw-2rem),28rem)] gap-0 overflow-y-auto p-5">
        <form noValidate onSubmit={(event) => void submit(event)}>
          <DialogHeader className="space-y-0">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">{title}</DialogTitle>
            <DialogDescription className="mt-2 text-[13px] leading-5">
              {t(item.kind === "agent" ? "cashmaxx.integrations.dialog.agentKind" : "cashmaxx.integrations.dialog.guardKind")}
            </DialogDescription>
          </DialogHeader>
          <div className="mt-5 grid gap-4">
            {item.fields.map((field) => (
              <FieldInput key={field.name} integrationId={item.id} field={field} value={values[field.name] ?? ""}
                error={errors[field.name] ? t(errors[field.name]) : undefined} disabled={busy}
                onChange={(value) => setValue(field.name, value)} />
            ))}
          </div>
          {error ? <div className="mt-4"><InlineNotice tone="error">{error}</InlineNotice></div> : null}
          <DialogFooter className="mt-6 gap-2">
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose} className="rounded-full">
              {t("cashmaxx.actions.cancel")}
            </Button>
            <Button type="submit" disabled={busy} className="rounded-full">
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              {t("cashmaxx.integrations.dialog.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function FieldInput({
  integrationId,
  field,
  value,
  error,
  disabled,
  onChange,
}: {
  integrationId: string;
  field: IntegrationField;
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const id = `cashmaxx-integration-${integrationId}-${field.name}`;
  const label = t(`cashmaxx.integrations.fields.${field.name}`, { defaultValue: field.label });
  const keep = field.secret && field.set;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [keep ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="grid gap-1.5">
      <label htmlFor={id} className="text-[12.5px] font-medium text-foreground">
        {label}
        {field.required && !keep ? (
          <span className="ml-1 text-muted-foreground">
            <span aria-hidden>*</span>
            <span className="sr-only">({t("cashmaxx.integrations.dialog.required")})</span>
          </span>
        ) : null}
      </label>
      {field.choices.length ? (
        <select
          id={id}
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
          className={cn(
            "h-10 w-full min-w-0 rounded-control border border-input bg-background px-3 text-[13px] text-foreground transition-colors",
            formControlFocusClassName,
            error && "border-destructive/60",
          )}
        >
          {field.required ? null : <option value="">{t("cashmaxx.integrations.dialog.choose")}</option>}
          {field.choices.map((choice) => (
            <option key={choice} value={choice}>
              {t(`cashmaxx.integrations.choices.${choice}`, { defaultValue: choice })}
            </option>
          ))}
        </select>
      ) : (
        <Input
          id={id}
          type={field.secret ? "password" : "text"}
          autoComplete={field.secret ? "new-password" : "off"}
          spellCheck={false}
          placeholder={keep ? t("cashmaxx.integrations.dialog.keepPlaceholder") : field.placeholder || undefined}
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
          className={cn("h-10 min-w-0 text-[14px]", field.secret && "font-mono", error && "border-destructive/60")}
        />
      )}
      {keep ? (
        <p id={hintId} className="text-[12px] leading-5 text-muted-foreground">
          {t("cashmaxx.integrations.dialog.keepSecret")}
        </p>
      ) : null}
      {error ? <p id={errorId} className="text-[12px] text-destructive dark:text-red-400">{error}</p> : null}
    </div>
  );
}

export function DisconnectIntegrationDialog({
  item,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  item: IntegrationItem | null;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (item: IntegrationItem) => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open={item !== null} onOpenChange={(open) => { if (!open && !busy) onCancel(); }}>
      {item ? (
        <DialogContent className="w-[min(calc(100vw-2rem),26rem)] gap-0 p-5">
          <DialogHeader className="space-y-0">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {t("cashmaxx.integrations.disconnect.title", { label: item.label })}
            </DialogTitle>
            <DialogDescription className="mt-2 text-[13px] leading-5">
              {t(item.category === "email"
                ? "cashmaxx.integrations.disconnect.emailBody"
                : "cashmaxx.integrations.disconnect.body")}
            </DialogDescription>
          </DialogHeader>
          {error ? <div className="mt-4"><InlineNotice tone="error">{error}</InlineNotice></div> : null}
          <DialogFooter className="mt-6 gap-2">
            <Button type="button" variant="ghost" disabled={busy} onClick={onCancel} className="rounded-full">
              {t("cashmaxx.actions.cancel")}
            </Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => onConfirm(item)} className="rounded-full">
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              {t("cashmaxx.integrations.disconnect.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}
