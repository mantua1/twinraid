import { useEffect, useState } from "react";

import { api } from "@/api/client";
import type { AppInfo, StatusSnapshot } from "@/api/types";

export interface DashboardData {
  info: AppInfo | null;
  status: StatusSnapshot | null;
  loading: boolean;
  infoFailed: boolean;
  statusFailed: boolean;
}

const INITIAL: DashboardData = {
  info: null,
  status: null,
  loading: true,
  infoFailed: false,
  statusFailed: false,
};

/** Lädt Info und Status einmalig. Live-Updates per SSE folgen in Phase 3. */
export function useDashboardData(): DashboardData {
  const [data, setData] = useState<DashboardData>(INITIAL);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    void Promise.allSettled([api.info(signal), api.status(signal)]).then(([info, status]) => {
      if (signal.aborted) {
        return;
      }
      setData({
        info: info.status === "fulfilled" ? info.value : null,
        status: status.status === "fulfilled" ? status.value : null,
        loading: false,
        infoFailed: info.status === "rejected",
        statusFailed: status.status === "rejected",
      });
    });

    return () => {
      controller.abort();
    };
  }, []);

  return data;
}
