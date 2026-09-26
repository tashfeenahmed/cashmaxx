import { KeyRound, Loader2 } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
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

import { CashmaxxError, createOwnerSession, needsOwner, type CashmaxxConnection } from "./api";
import type { OwnerSession } from "./types";

/** Thrown when the owner closes the PIN dialog. Callers treat it as a no-op. */
export class OwnerPromptCancelled extends Error {
  constructor() {
    super("owner PIN prompt cancelled");
    this.name = "OwnerPromptCancelled";
  }
}

export function isPromptCancelled(error: unknown): boolean {
  return error instanceof OwnerPromptCancelled;
}

interface OwnerSessionContextValue {
  /** Runs an owner-scoped call. Prompts for the PIN when there is no live session, and once
   *  more if the guard rejects the session (401/403). */
  runAsOwner: <T>(action: (session: string) => Promise<T>) => Promise<T>;
  hasSession: boolean;
  clearSession: () => void;
}

const OwnerSessionContext = createContext<OwnerSessionContextValue | null>(null);

// Refuse a session this close to expiry so a call does not race it.
const EXPIRY_SKEW_MS = 15_000;

function sessionIsLive(session: OwnerSession | null, now = Date.now()): session is OwnerSession {
  if (!session) return false;
  if (!session.expires_at) return true;
  const expires = Date.parse(session.expires_at);
  return !Number.isFinite(expires) || expires - EXPIRY_SKEW_MS > now;
}

/**
 * Holds the owner session in React state only: never localStorage/sessionStorage, so it goes
 * away with the tab (or when the Cashmaxx section unmounts). The PIN itself is never kept.
 */
export function OwnerSessionProvider({
  connection,
  children,
}: {
  connection: CashmaxxConnection;
  children: ReactNode;
}) {
  const [session, setSession] = useState<OwnerSession | null>(null);
  const sessionRef = useRef<OwnerSession | null>(null);
  const [promptOpen, setPromptOpen] = useState(false);
  const pendingRef = useRef<{
    resolve: (session: string) => void;
    reject: (error: unknown) => void;
  } | null>(null);
  const promptRef = useRef<Promise<string> | null>(null);

  const store = useCallback((next: OwnerSession | null) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  const prompt = useCallback((): Promise<string> => {
    if (promptRef.current) return promptRef.current;
    const promise = new Promise<string>((resolve, reject) => {
      pendingRef.current = { resolve, reject };
    }).finally(() => {
      promptRef.current = null;
      pendingRef.current = null;
    });
    promptRef.current = promise;
    setPromptOpen(true);
    return promise;
  }, []);

  const runAsOwner = useCallback(async <T,>(action: (session: string) => Promise<T>): Promise<T> => {
    const current = sessionRef.current;
    const first = sessionIsLive(current) ? current.session : await prompt();
    try {
      return await action(first);
    } catch (error) {
      if (!needsOwner(error)) throw error;
      store(null);
      const retry = await prompt();
      return action(retry);
    }
  }, [prompt, store]);

  const clearSession = useCallback(() => store(null), [store]);

  const value = useMemo<OwnerSessionContextValue>(() => ({
    runAsOwner,
    hasSession: sessionIsLive(session),
    clearSession,
  }), [runAsOwner, session, clearSession]);

  return (
    <OwnerSessionContext.Provider value={value}>
      {children}
      <OwnerPinDialog
        open={promptOpen}
        connection={connection}
        onSession={(next) => {
          store(next);
          setPromptOpen(false);
          pendingRef.current?.resolve(next.session);
        }}
        onCancel={() => {
          setPromptOpen(false);
          pendingRef.current?.reject(new OwnerPromptCancelled());
        }}
      />
    </OwnerSessionContext.Provider>
  );
}

export function useOwnerSession(): OwnerSessionContextValue {
  const ctx = useContext(OwnerSessionContext);
  if (!ctx) throw new Error("useOwnerSession must be used within OwnerSessionProvider");
  return ctx;
}

function OwnerPinDialog({
  open,
  connection,
  onSession,
  onCancel,
}: {
  open: boolean;
  connection: CashmaxxConnection;
  onSession: (session: OwnerSession) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setPin("");
    setError(null);
    setBusy(false);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!pin.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await createOwnerSession(connection, pin.trim());
      reset();
      onSession(next);
    } catch (err) {
      setBusy(false);
      setPin("");
      if (err instanceof CashmaxxError && (err.status === 401 || err.status === 403)) {
        setError(t("cashmaxx.pin.wrong"));
      } else if (err instanceof CashmaxxError && err.status === 429) {
        setError(t("cashmaxx.pin.rateLimited"));
      } else if (err instanceof CashmaxxError && err.status === 503) {
        setError(t("cashmaxx.guard.unavailableTitle"));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !busy) {
          reset();
          onCancel();
        }
      }}
    >
      <DialogContent className="w-[min(calc(100vw-2rem),24rem)] gap-0 p-5">
        <form onSubmit={(event) => void submit(event)}>
          <DialogHeader className="space-y-0">
            <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-full bg-muted text-muted-foreground max-sm:mx-auto">
              <KeyRound className="h-4 w-4" aria-hidden />
            </div>
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {t("cashmaxx.pin.title")}
            </DialogTitle>
            <DialogDescription className="mt-2 text-[13px] leading-5">
              {t("cashmaxx.pin.description")}
            </DialogDescription>
          </DialogHeader>
          <label htmlFor="cashmaxx-owner-pin" className="mt-5 block text-[12.5px] font-medium text-foreground">
            {t("cashmaxx.pin.label")}
          </label>
          <Input
            id="cashmaxx-owner-pin"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            autoFocus
            value={pin}
            disabled={busy}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "cashmaxx-owner-pin-error" : undefined}
            onChange={(event) => setPin(event.target.value)}
            className="mt-1.5 h-10 text-[14px] tracking-[0.2em]"
          />
          {error ? (
            <p id="cashmaxx-owner-pin-error" role="alert" className="mt-2 text-[12.5px] text-destructive">
              {error}
            </p>
          ) : null}
          <DialogFooter className="mt-6 gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                reset();
                onCancel();
              }}
              className="rounded-full"
            >
              {t("cashmaxx.actions.cancel")}
            </Button>
            <Button type="submit" disabled={busy || !pin.trim()} className="rounded-full">
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              {t("cashmaxx.pin.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
