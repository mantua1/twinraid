import { useEffect, useState } from "react";

import { api, UnauthorizedError } from "@/api/client";
import type { AppInfo, AuditEntry, StatusSnapshot, TlsInfo } from "@/api/types";

export interface DashboardData {
  info: AppInfo | null;
  status: StatusSnapshot | null;
  audit: AuditEntry[];
  tls: TlsInfo | null;
  loading: boolean;
  infoFailed: boolean;
  statusFailed: boolean;
}

const INITIAL: DashboardData = {
  info: null,
  status: null,
  audit: [],
  tls: null,
  loading: true,
  infoFailed: false,
  statusFailed: false,
};

/** Lädt die Daten nach dem Login einmalig. Live-Updates per SSE folgen in Phase 3. */
export function useDashboardData(onUnauthorized: () => void): DashboardData {
  const [data, setData] = useState<DashboardData>(INITIAL);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    void Promise.allSettled([
      api.info(signal),
      api.status(signal),
      api.audit(signal),
      api.tls(signal),
    ]).then(([info, status, audit, tls]) => {
      if (signal.aborted) {
        return;
      }
      const results = [info, status, audit, tls];
      if (results.some((r) => r.status === "rejected" && r.reason instanceof UnauthorizedError)) {
        onUnauthorized();
        return;
      }
      setData({
        info: info.status === "fulfilled" ? info.value : null,
        status: status.status === "fulfilled" ? status.value : null,
        audit: audit.status === "fulfilled" ? audit.value : [],
        tls: tls.status === "fulfilled" ? tls.value : null,
        loading: false,
        infoFailed: info.status === "rejected",
        statusFailed: status.status === "rejected",
      });
    });

    return () => {
      controller.abort();
    };
  }, [onUnauthorized]);

  return data;
}
