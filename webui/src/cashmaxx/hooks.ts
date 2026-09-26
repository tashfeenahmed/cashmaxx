import { useCallback, useEffect, useRef, useState } from "react";

import { usePageVisibility } from "@/hooks/usePageVisibility";

export interface CashmaxxQuery<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * Loads `loader` when `key` changes. Keeps the previous data while reloading so the page does
 * not flash, and drops out-of-order responses.
 */
export function useCashmaxxQuery<T>(loader: () => Promise<T>, key: string): CashmaxxQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const seqRef = useRef(0);

  const reload = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const next = await loaderRef.current();
      if (seq !== seqRef.current) return;
      setData(next);
      setError(null);
    } catch (err) {
      if (seq !== seqRef.current) return;
      setError(err);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [key, reload]);

  useEffect(() => () => {
    seqRef.current += 1;
  }, []);

  return { data, error, loading, reload };
}

/** Calls `tick` every `intervalMs` while the page is visible. */
export function useVisiblePolling(tick: () => void, intervalMs: number, enabled = true): void {
  const visible = usePageVisibility();
  const tickRef = useRef(tick);
  tickRef.current = tick;
  useEffect(() => {
    if (!visible || !enabled) return;
    const id = window.setInterval(() => tickRef.current(), intervalMs);
    return () => window.clearInterval(id);
  }, [visible, enabled, intervalMs]);
}
