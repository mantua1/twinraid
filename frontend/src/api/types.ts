// Antworttypen der REST-API (Spiegel von backend/src/twinraid/web/schemas.py und shared/status.py).

export const RESOURCE_STATES = ["online", "offline", "starting", "stopping", "unknown"] as const;
export type ResourceState = (typeof RESOURCE_STATES)[number];

export interface StatusSnapshot {
  source: ResourceState;
  target: ResourceState;
  vm: ResourceState;
  checked_at: string | null;
}

export interface AppInfo {
  version: string;
  mode: "mock" | "real";
  default_language: "de" | "en";
  names: {
    source: string;
    target: string;
    vm: string;
  };
}
