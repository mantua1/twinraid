import { useCallback, useEffect, useState } from "react";

import { api, UnauthorizedError } from "@/api/client";
import type { AppInfo, AuditEntry, TlsInfo } from "@/api/types";
import { useStatusStream, type StatusStream } from "@/hooks/useStatusStream";

export interface DashboardData {
  info: AppInfo | null;
  stream: StatusStream;
  audit: AuditEntry[];
  tls: TlsInfo | null;
  loading: boolean;
  infoFailed: boolean;
  // Bestätigt den Hinweis zum CA-Wechsel und lädt Zertifikat und Verlauf neu.
  acknowledgeCaChange: () => Promise<void>;
}

interface StaticData {
  info: AppInfo | null;
  audit: AuditEntry[];
  tls: TlsInfo | null;
  loading: boolean;
  infoFailed: boolean;
}

const INITIAL: StaticData = {
  info: null,
  audit: [],
  tls: null,
  loading: true,
  infoFailed: false,
};

/** Lädt die Daten nach dem Login einmalig; der Status kommt live per SSE. */
export function useDashboardData(onUnauthorized: () => void): DashboardData {
  const [data, setData] = useState<StaticData>(INITIAL);
  const stream = useStatusStream(onUnauthorized);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    void Promise.allSettled([api.info(signal), api.audit(signal), api.tls(signal)]).then(
      ([info, audit, tls]) => {
        if (signal.aborted) {
          return;
        }
        const results = [info, audit, tls];
        if (results.some((r) => r.status === "rejected" && r.reason instanceof UnauthorizedError)) {
          onUnauthorized();
          return;
        }
        setData({
          info: info.status === "fulfilled" ? info.value : null,
          audit: audit.status === "fulfilled" ? audit.value : [],
          tls: tls.status === "fulfilled" ? tls.value : null,
          loading: false,
          infoFailed: info.status === "rejected",
        });
      },
    );

    return () => {
      controller.abort();
    };
  }, [onUnauthorized]);

  const acknowledgeCaChange = useCallback(async () => {
    try {
      await api.acknowledgeCaChange();
      const [tls, audit] = await Promise.all([api.tls(), api.audit()]);
      setData((previous) => ({ ...previous, tls, audit }));
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      throw error;
    }
  }, [onUnauthorized]);

  return { ...data, stream, acknowledgeCaChange };
}
