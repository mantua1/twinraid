import { useEffect, useState } from "react";

import { api, EVENTS_URL } from "@/api/client";
import type { StatusSnapshot } from "@/api/types";

export type StreamState = "connecting" | "live" | "workerUnavailable" | "reconnecting";

export interface StatusStream {
  status: StatusSnapshot | null;
  state: StreamState;
}

// Nach einem endgültigen Abbruch (z. B. Neustart des Web-Prozesses) neu verbinden.
const RECONNECT_DELAY_MS = 5000;

/**
 * Live-Status per SSE (Konzept 4.1). Der Datenstrom verlängert die Session nicht.
 * Endet die Session, meldet der Server `session_ended`; dann geht es zurück zum Login.
 */
export function useStatusStream(onUnauthorized: () => void): StatusStream {
  const [stream, setStream] = useState<StatusStream>({ status: null, state: "connecting" });

  useEffect(() => {
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const stop = () => {
      stopped = true;
      source?.close();
      if (timer !== null) {
        clearTimeout(timer);
      }
    };

    const checkSession = () => {
      void api
        .authState()
        .then((state) => {
          if (stopped) {
            return;
          }
          if (!state.authenticated) {
            stop();
            onUnauthorized();
            return;
          }
          timer = setTimeout(connect, RECONNECT_DELAY_MS);
        })
        .catch(() => {
          if (!stopped) {
            timer = setTimeout(connect, RECONNECT_DELAY_MS);
          }
        });
    };

    const connect = () => {
      if (stopped) {
        return;
      }
      source = new EventSource(EVENTS_URL);
      source.addEventListener("status", (event) => {
        const status = JSON.parse((event as MessageEvent<string>).data) as StatusSnapshot;
        setStream({ status, state: "live" });
      });
      source.addEventListener("worker_unavailable", () => {
        setStream((previous) => ({ ...previous, state: "workerUnavailable" }));
      });
      source.addEventListener("session_ended", () => {
        stop();
        onUnauthorized();
      });
      source.onerror = () => {
        if (stopped || source === null) {
          return;
        }
        setStream((previous) => ({ ...previous, state: "reconnecting" }));
        // CLOSED: Der Browser versucht es nicht mehr selbst, z. B. nach 401.
        if (source.readyState === EventSource.CLOSED) {
          source.close();
          checkSession();
        }
      };
    };

    connect();
    return stop;
  }, [onUnauthorized]);

  return stream;
}
