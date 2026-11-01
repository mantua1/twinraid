// Wächter auf Klein in der Kachel Klein, der Kachel Sync und den Einstellungen (Konzept 3.6).
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { describe, expect, it } from "vitest";

import type { GuardDetails, SourceGuardStatus, StatusSnapshot } from "@/api/types";
import {
  dataset,
  GUARD_ACTIVE,
  GUARD_DETAILS,
  installFakeApi,
  latestStream,
  STATUS,
  type FakeServer,
} from "@/test/fakeApi";

import { App } from "../App";

const GIB = 1024 ** 3;

function withGuard(guard: Partial<SourceGuardStatus>, notices: StatusSnapshot["notices"] = []) {
  return { ...STATUS, notices, source_guard: { ...GUARD_ACTIVE, ...guard } };
}

function withDetails(details: Partial<GuardDetails>) {
  return withGuard({ details: { ...GUARD_DETAILS, ...details } });
}

async function openDashboard(overrides: Partial<FakeServer> = {}) {
  installFakeApi({ authenticated: true, ...overrides });
  const user = userEvent.setup();
  render(<App />);
  const source = await screen.findByTestId("tile-source");
  await within(source).findByTestId("source-guard");
  return { user, source, sync: screen.getByTestId("tile-sync") };
}

function guardState(source: HTMLElement) {
  return within(source).getByTestId("guard-state");
}

describe("tile Klein", () => {
  it("shows an active guard with its allowed containers", async () => {
    const { source } = await openDashboard();
    const state = guardState(source);
    expect(state).toHaveAttribute("data-state", "active");
    expect(state).toHaveTextContent("Wächter: aktiv · 1 Container freigegeben");
    // Kein laufendes Alter des Herzschlags in der Kachel (D1); es steht in den Einstellungen.
    expect(state).not.toHaveTextContent("Herzschlag");
    expect(within(source).getByTestId("guard-active-hint")).toHaveTextContent(
      "Status wird alle 5 s geprüft.",
    );
    expect(state).not.toHaveClass("text-destructive");
    expect(within(source).getByTestId("guard-zfs")).toHaveTextContent(
      "ZFS: 1 Dataset, 680 GiB frei auf mainpool.",
    );
    expect(within(source).queryByTestId("guard-warning")).toBeNull();
  });

  it("shows a guard that is not set up neutrally", async () => {
    const { source } = await openDashboard({
      status: withGuard({ state: "not_mounted", heartbeat_age_seconds: null, details: null }),
    });
    const state = guardState(source);
    expect(state).toHaveTextContent(
      "Wächter nicht eingerichtet. Die Schritte stehen in docs/EINRICHTUNG.md.",
    );
    expect(state).toHaveClass("text-muted-foreground");
    expect(state).not.toHaveClass("text-destructive");
  });

  it("shows an inactive guard red with the time of the last heartbeat", async () => {
    const { source } = await openDashboard({
      status: withGuard({
        state: "inactive",
        heartbeat_at: "2026-10-13T11:58:00Z",
        heartbeat_age_seconds: null,
        details: null,
      }),
    });
    const state = guardState(source);
    expect(state).toHaveClass("text-destructive");
    expect(state).toHaveTextContent(/^Wächter nicht aktiv seit .+\./);
    expect(state).toHaveTextContent(
      "Auf Unraid Klein prüfen mit: /bin/bash /boot/config/twinraid/source-guard.sh status",
    );
    expect(within(source).queryByTestId("guard-zfs")).toBeNull();
  });

  it("names an invalid configuration with its line", async () => {
    const { source } = await openDashboard({
      status: withGuard({
        state: "error",
        error: "config_invalid",
        error_detail: "bad_value",
        config_line: 7,
      }),
    });
    const state = guardState(source);
    expect(state).toHaveClass("text-destructive");
    expect(state).toHaveTextContent("Wächter: source-guard.conf ist ungültig");
    expect(state).toHaveTextContent("Ein Wert ist ungültig");
    expect(state).toHaveTextContent("Zeile 7 in source-guard.conf.");
  });

  it("explains a directory in place of the order file", async () => {
    const { source } = await openDashboard({
      status: withGuard({
        state: "misconfigured",
        setup_problem: "request_is_directory",
        error: "request_not_a_file",
        error_detail: "request_is_directory",
      }),
    });
    const state = guardState(source);
    expect(state).toHaveClass("text-destructive");
    expect(state).toHaveTextContent("Wächter starten, dann TwinRaid neu starten");
  });

  it("names a broken status file", async () => {
    const { source } = await openDashboard({
      status: withGuard({
        state: "invalid_status",
        status_problem: "protocol_mismatch",
        heartbeat_age_seconds: null,
        details: null,
      }),
    });
    expect(guardState(source)).toHaveTextContent(
      "Status des Wächters ungültig: Der Wächter spricht ein anderes Protokoll",
    );
  });

  it("shows an open run and containers that did not start again", async () => {
    const { source } = await openDashboard({
      status: withDetails({
        run: {
          opened_at: "2026-10-13T11:59:00Z",
          heartbeat_deadline: "2026-10-13T12:01:00Z",
          max_deadline: "2026-10-13T12:19:00Z",
          stopped: ["twinraid-testcontainer"],
        },
        last_run: {
          opened_at: "2026-10-13T11:00:00Z",
          closed_at: "2026-10-13T11:03:00Z",
          reason: "deadman_heartbeat",
          containers: [
            { name: "twinraid-testcontainer", result: "started" },
            { name: "zweiter", result: "start_failed" },
            { name: "dritter", result: "gone" },
          ],
        },
      }),
    });
    expect(within(source).getByTestId("guard-run")).toHaveTextContent(
      /^1 Container angehalten \(twinraid-testcontainer\), spätestens wieder an um \d{1,2}:\d{2}\.$/,
    );
    expect(within(source).getByTestId("guard-last-run")).toHaveTextContent(
      "über den Totmannschalter: TwinRaid hat sich nicht mehr gemeldet.",
    );
    const warnings = within(source).getAllByTestId("guard-warning");
    expect(warnings.map((warning) => warning.textContent)).toEqual([
      "zweiter ist nach dem Lauf nicht wieder gestartet. In Unraid prüfen und von Hand starten.",
      "dritter gibt es nach dem Lauf nicht mehr. In Unraid prüfen.",
    ]);
  });

  it("warns about compression only when it is off", async () => {
    const zfs = GUARD_DETAILS.zfs;
    const { source } = await openDashboard({
      status: withDetails({
        zfs: {
          ...zfs,
          datasets: [
            dataset("eins", true, { referenced: GIB, compression: "on" }),
            dataset("zwei", true, { referenced: GIB, compression: "lz4" }),
            dataset("drei", true, { referenced: GIB, compression: "zstd" }),
            dataset("vier", true, { referenced: GIB, compression: "off" }),
            dataset("fuenf", false),
          ],
        },
      }),
    });
    const warnings = within(source).getAllByTestId("guard-warning");
    expect(warnings.map((warning) => warning.textContent)).toEqual([
      "Bei mainpool/vier ist die Kompression aus. In Unraid in den Einstellungen des Pools „Compression on“ wählen.",
      "Das Dataset mainpool/fuenf fehlt.",
    ]);
  });

  it("names the own dataset only when appdata is mirrored", async () => {
    const zfs = GUARD_DETAILS.zfs;
    const { source } = await openDashboard({
      status: withDetails({ zfs: { ...zfs, self_dataset: "missing" } }),
    });
    // Ohne appdata in der Liste des Wächters hat der Hinweis bis Test Z4 keine Bedeutung.
    expect(within(source).queryByTestId("guard-warning")).toBeNull();
    act(() => {
      latestStream().emit(
        "status",
        withDetails({
          zfs: {
            ...zfs,
            datasets: [
              ...zfs.datasets,
              dataset("appdata", true, { referenced: GIB, compression: "on" }),
            ],
            self_dataset: "missing",
          },
        }),
      );
    });
    expect(within(source).getByTestId("guard-warning")).toHaveTextContent(
      "Das eigene Dataset mainpool/appdata/twinraid fehlt",
    );
  });

  it("warns when ZFS is not available", async () => {
    const { source } = await openDashboard({
      status: withDetails({
        zfs: {
          state: "unavailable",
          pool_used: null,
          pool_available: null,
          datasets: [dataset("twinraidtest", null)],
          self_dataset: "unknown",
        },
      }),
    });
    expect(within(source).queryByTestId("guard-zfs")).toBeNull();
    expect(within(source).getByTestId("guard-warning")).toHaveTextContent(
      "ZFS nicht verfügbar: mainpool fehlt oder zfs antwortet nicht.",
    );
  });

  it("shows the notices about Klein only in its tile", async () => {
    const { source } = await openDashboard({
      status: withGuard({}, [
        "source_connect_plugin",
        "source_flash_backup",
        "target_flash_backup",
      ]),
    });
    const notices = within(source).getAllByTestId("status-notice");
    expect(notices.map((notice) => notice.dataset.notice)).toEqual([
      "source_connect_plugin",
      "source_flash_backup",
    ]);
    expect(notices[0]).toHaveTextContent(
      "Auf Unraid Klein ist das Plugin Unraid Connect installiert.",
    );
    const target = screen.getByTestId("tile-target");
    expect(
      within(target)
        .getAllByTestId("status-notice")
        .map((notice) => notice.dataset.notice),
    ).toEqual(["target_flash_backup"]);
  });

  it("explains it in English", async () => {
    const { user, source } = await openDashboard();
    await user.click(screen.getByRole("button", { name: "Englisch" }));
    expect(guardState(source)).toHaveTextContent(
      "Guard: active · 1 container allowedStatus is checked every 5 s.",
    );
  });
});

describe("tile Sync", () => {
  it("names the earliest next sync, rounded up to the minute", async () => {
    const { sync } = await openDashboard({
      status: withDetails({ run_allowed_at: "2026-10-13T12:29:10Z" }),
    });
    const expected = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date("2026-10-13T12:30:00Z"),
    );
    expect(within(sync).getByTestId("next-sync")).toHaveTextContent(
      `Nächster Sync frühestens um ${expected}.`,
    );
  });

  it("has no such line without a pause", async () => {
    const { sync } = await openDashboard();
    expect(within(sync).queryByTestId("next-sync")).toBeNull();
  });
});

describe("settings", () => {
  it("lists the guard read-only", async () => {
    const { user } = await openDashboard({
      status: withDetails({
        allowed: [
          { name: "twinraid-testcontainer", state: "running" },
          { name: "weg", state: "missing" },
        ],
        rejected_count: 3,
      }),
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const card = await screen.findByTestId("settings-source-guard");
    expect(card).toHaveTextContent("Wächter auf Unraid Klein");
    expect(within(card).getByTestId("guard-allowed")).toHaveTextContent(
      "twinraid-testcontainer: läuftweg: nicht gefunden",
    );
    expect(within(card).getByTestId("guard-datasets")).toHaveTextContent(
      "mainpool/twinraidtest: 2 GiB · Kompression on",
    );
    expect(within(card).getByTestId("guard-rejected")).toHaveTextContent("3");
    expect(card).toHaveTextContent("nicht eingetragen (target_host in source-guard.conf)");
    // Das genaue Alter des Herzschlags steht nur hier (D1).
    expect(within(card).getByTestId("guard-heartbeat")).toHaveTextContent("vor 4 Sekunden");
    expect(within(card).getByTestId("guard-key")).toHaveTextContent(
      "fehlt (source-guard.sh init-key)",
    );
    expect(within(card).getByTestId("guard-known-host")).toHaveTextContent(
      "nicht bestätigt (source-guard.sh trust-target)",
    );
  });

  it("lists snapshots, base and bookmark per dataset", async () => {
    const { user } = await openDashboard({
      status: withDetails({
        target_configured: true,
        target_key: true,
        target_known_host: true,
        zfs: {
          ...GUARD_DETAILS.zfs,
          datasets: [
            dataset("twinraidtest", true, {
              snapshots: 3,
              oldest_snapshot_at: "2026-10-10T08:00:00Z",
              base_created_at: "2026-10-12T08:00:00Z",
              bookmark: true,
            }),
            dataset("neu", true, { snapshots: 0 }),
          ],
        },
      }),
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const card = await screen.findByTestId("settings-source-guard");
    const base = new Intl.DateTimeFormat("de", { dateStyle: "short", timeStyle: "medium" }).format(
      new Date("2026-10-12T08:00:00Z"),
    );
    expect(within(card).getByTestId("guard-datasets")).toHaveTextContent(
      `mainpool/twinraidtest: 2 GiB · Kompression on · 3 eigene Snapshots · Basis vom ${base} · mit Lesezeichen` +
        "mainpool/neu: 2 GiB · Kompression on · 0 eigene Snapshots · noch keine Basis",
    );
    expect(card).toHaveTextContent("eingetragen (SSH-Port 22)");
    expect(within(card).getByTestId("guard-key")).toHaveTextContent("vorhanden");
    expect(within(card).getByTestId("guard-known-host")).toHaveTextContent("bestätigt");
  });

  it("explains a guard that is not set up", async () => {
    const { user } = await openDashboard({
      status: withGuard({ state: "not_mounted", heartbeat_age_seconds: null, details: null }),
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("settings-source-guard")).toHaveTextContent(
      "Wächter nicht eingerichtet.",
    );
  });
});
