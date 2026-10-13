// Typisierter Client für die REST-API. Anfragen gehen nur an den eigenen Server.
import type { AppInfo, StatusSnapshot } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`API request failed with status ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  info: (signal?: AbortSignal) => getJson<AppInfo>("/api/info", signal),
  status: (signal?: AbortSignal) => getJson<StatusSnapshot>("/api/status", signal),
};
