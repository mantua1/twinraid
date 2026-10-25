// Kachel Sync und Einstellungen zum Probelauf (Konzept 4.1, 4.3 und D5, ab v0.5.1).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { describe, expect, it } from "vitest";

import type { StatusSnapshot, SyncResult, SyncStatus } from "@/api/types";
import {
  actionsSnapshot,
  CHECK,
  CSRF,
  GUARD_ACTIVE,
  GUARD_DETAILS,
  INFO,
  installFakeApi,
  latestStream,
  REPORT,
  SHARE_REPORT,
  STATUS,
  type FakeServer,
} from "@/test/fakeApi";

import { App } from "../App";

const JOB_ID = "0123456789abcdef0123456789abcdef";
const GIB = 1024 ** 3;

function withSync(sync: Partial<SyncStatus>, extra: Partial<StatusSnapshot> = {}): StatusSnapshot {
  return { ...STATUS, ...extra, sync: { ...STATUS.sync, ...sync } };
}

function result(outcome: SyncResult["outcome"], reason: SyncResult["reason"] = null): SyncResult {
  return { kind: "dry_run", outcome, reason, finished_at: "2026-10-13T12:01:05Z" };
}

async function openDashboard(overrides: Partial<FakeServer> = {}) {
  const server = installFakeApi({ authenticated: true, ...overrides });
  const user = userEvent.setup();
  render(<App />);
  const sync = await screen.findByTestId("tile-sync");
  await within(sync).findByRole("button", { name: "Nur Probelauf" });
  return { server, user, sync };
}

function emit(status: StatusSnapshot) {
  act(() => {
    latestStream().emit("status", status);
  });
}

describe("buttons", () => {
  it("starts a dry run; the real sync comes with v0.5.2", async () => {
    const { server, user, sync } = await openDashboard();
    const dryRun = within(sync).getByRole("button", { name: "Nur Probelauf" });
    const start = within(sync).getByRole("button", { name: "Jetzt synchronisieren" });
    expect(start).toBeDisabled();
    expect(within(sync).getByTestId("action-hint")).toHaveTextContent(
      "Jetzt synchronisieren: kommt mit v0.5.2",
    );
    expect(within(sync).getByTestId("sync-schedule")).toHaveTextContent(
      "Zeitplan aus (ab Phase 6).",
    );
    expect(within(sync).getByText("Noch kein Probelauf.")).toBeInTheDocument();
    await user.click(dryRun);
    expect(server.actions).toEqual(["sync_dry_run"]);
    const call = server.calls.find((entry) => entry.url === "/api/actions/sync_dry_run");
    expect(call?.headers.get("X-CSRF-Token")).toBe(CSRF);
  });

  it("explains why the dry run is locked", async () => {
    const { sync } = await openDashboard({
      status: {
        ...STATUS,
        actions: actionsSnapshot({ sync_dry_run: "target_offline" }),
      },
    });
    expect(within(sync).getByRole("button", { name: "Nur Probelauf" })).toBeDisabled();
    expect(within(sync).getAllByTestId("action-hint")[0]).toHaveTextContent(
      "Nur Probelauf: Unraid Groß ist aus, und Wecken ist nicht freigeschaltet (actions.wake)",
    );
    emit({ ...STATUS, actions: actionsSnapshot({ sync_dry_run: "levels_mismatch" }) });
    expect(within(sync).getAllByTestId("action-hint")[0]).toHaveTextContent(
      "Nur Probelauf: sync.shares in twinraid.yaml passt nicht zu den Zeilen dataset in source-guard.conf auf Unraid Klein",
    );
    emit({ ...STATUS, actions: actionsSnapshot({ sync_dry_run: "source_guard_inactive" }) });
    expect(within(sync).getAllByTestId("action-hint")[0]).toHaveTextContent(
      "der Wächter auf Unraid Klein ist nicht aktiv oder ZFS ist dort nicht verfügbar",
    );
  });

  it("names a refusal when the dry run is sent", async () => {
    const { user, sync } = await openDashboard({ actionRejection: "self_dataset_missing" });
    await user.click(within(sync).getByRole("button", { name: "Nur Probelauf" }));
    expect(await within(sync).findByRole("alert")).toHaveTextContent(
      "Nicht ausgeführt: appdata wird gespiegelt, aber das eigene Dataset mainpool/appdata/twinraid fehlt",
    );
  });
});

describe("running dry run", () => {
  const running = {
    kind: "dry_run" as const,
    job_id: JOB_ID,
    step: "preparing" as const,
    started_at: "2026-10-13T12:00:00Z",
  };

  it("shows the step and cancels with the job id of the worker", async () => {
    const { server, user, sync } = await openDashboard({
      status: withSync({ running }, { actions: actionsSnapshot({ sync_dry_run: "busy" }) }),
    });
    expect(within(sync).getByTestId("sync-running")).toHaveTextContent(
      "Probelauf läuft: Änderungen werden gezählt und Unraid Groß geprüft …",
    );
    expect(within(sync).getByRole("button", { name: "Nur Probelauf" })).toBeDisabled();
    await user.click(within(sync).getByRole("button", { name: "Abbrechen" }));
    await waitFor(() => {
      expect(server.cancelled).toEqual([JOB_ID]);
    });
    const call = server.calls.find((entry) => entry.url === `/api/sync/${JOB_ID}/cancel`);
    expect(call?.method).toBe("POST");
    expect(call?.headers.get("X-CSRF-Token")).toBe(CSRF);
    expect(within(sync).queryByRole("alert")).toBeNull();
  });

  it("says so when cancelling did not work", async () => {
    const { user, sync } = await openDashboard({
      status: withSync({ running }),
      cancelAccepted: false,
    });
    await user.click(within(sync).getByRole("button", { name: "Abbrechen" }));
    expect(await within(sync).findByRole("alert")).toHaveTextContent(
      "Abbrechen hat nicht geklappt. Bitte erneut versuchen.",
    );
    // Mit dem Ende des Laufs verschwindet die Meldung.
    emit(withSync({ running: null, last: result("cancelled") }));
    expect(within(sync).queryByRole("alert")).toBeNull();
  });

  it.each([
    ["waking", "Unraid Groß wird geweckt …"],
    ["waiting_for_vm_service", "Wartet, bis Unraid Groß bereit ist …"],
    ["snapshot", "Snapshot auf Unraid Klein wird angelegt …"],
    ["closing", "Job wird geschlossen …"],
  ] as const)("names the step %s", async (step, text) => {
    const { sync } = await openDashboard({ status: withSync({ running: { ...running, step } }) });
    expect(within(sync).getByTestId("sync-running")).toHaveTextContent(text);
  });
});

describe("result", () => {
  it("shows the report per share", async () => {
    const { sync } = await openDashboard({
      status: withSync({ last: result("dry_run_success"), report: REPORT }),
    });
    const last = within(sync).getByTestId("sync-last");
    expect(last).toHaveTextContent("Probelauf abgeschlossen; Senden wäre möglich.");
    expect(last.querySelector("p")).not.toHaveClass("text-destructive");
    const row = within(sync).getByTestId("sync-share");
    expect(row).toHaveTextContent("twinraidtestinkrementell1234562350 MiBvorhanden · 5 Snapshots");
    expect(within(sync).getByTestId("sync-send-ready")).toHaveTextContent("Senden wäre möglich.");
    expect(within(sync).getByTestId("sync-target-space")).toHaveTextContent(
      "Unraid Groß: 600 GiB von 860 GiB frei, Reserve 15 %.",
    );
    expect(within(sync).queryByTestId("sync-reason")).toBeNull();
  });

  it("names deletions that need approval and what blocks sending", async () => {
    const report = {
      ...REPORT,
      awaiting_confirm: true,
      confirm_reasons: ["shrunk" as const, "deletions" as const],
      send_ready: false,
      blockers: ["clone_active" as const, "space_low" as const],
      shares: [
        { ...SHARE_REPORT, deleted: 1500, referenced: 300 * 1024 ** 2 },
        {
          ...SHARE_REPORT,
          name: "neu",
          mode: "full" as const,
          base_type: null,
          mirror: { state: "absent" as const, props: null, count: 0, newest_received: null },
        },
        {
          ...SHARE_REPORT,
          name: "fremd",
          mode: null,
          base_type: null,
          problem: "target_exists" as const,
          deleted: null,
          modified: null,
          added: null,
          renamed: null,
          size: null,
          mirror: { state: "foreign" as const, props: null, count: null, newest_received: null },
        },
      ],
    };
    const { sync } = await openDashboard({
      status: withSync({ last: result("target_refused", "clone_active"), report }),
    });
    expect(within(sync).getByTestId("sync-last").querySelector("p")).toHaveClass(
      "text-destructive",
    );
    expect(within(sync).getByTestId("sync-reason")).toHaveTextContent(
      "Grund: Auf Unraid Groß besteht ein Klon eines Spiegels (Notbetrieb?).",
    );
    const rows = within(sync).getAllByTestId("sync-share");
    expect(rows[0]).toHaveTextContent("1.500");
    expect(rows[1]).toHaveTextContent("neuvollständig (erster Lauf)");
    expect(rows[1]).toHaveTextContent("noch keiner · 0 Snapshots");
    expect(rows[2]).toHaveTextContent("fremdfremdes Dataset auf dem Ziel–––––fremdes Dataset");
    const blockers = within(sync).getByTestId("sync-blockers");
    expect(blockers).toHaveTextContent("Senden wäre gesperrt:");
    expect(blockers).toHaveTextContent(
      "Auf Unraid Groß ist zu wenig Platz (Reserve laut mirror.conf).",
    );
    expect(within(sync).getByTestId("sync-confirm")).toHaveTextContent(
      "Ein echter Sync würde vorher nach einer Freigabe fragen:" +
        "Ein Share ist auf weniger als die Hälfte geschrumpft." +
        "Mehr Löschungen als max_deletions in source-guard.conf.",
    );
  });

  it.each([
    [
      result("target_check_failed", "host_untrusted"),
      "Grund: Hostkey von Unraid Groß nicht bestätigt (source-guard.sh trust-target)",
    ],
    [result("source_guard_refused", "job_open"), "Grund: auf Unraid Klein ist noch ein Job offen"],
    [result("source_guard_refused", "opened"), "Grund: Ergebnis des Wächters: opened"],
    [
      result("target_refused", "target_exists"),
      "Grund: Auf Unraid Groß liegt an der Stelle eines Spiegels ein fremdes Dataset. Ansehen und von Hand entfernen (docs/EINRICHTUNG.md).",
    ],
  ])("names the reason of %o", async (last, text) => {
    const { sync } = await openDashboard({ status: withSync({ last }) });
    expect(within(sync).getByTestId("sync-reason")).toHaveTextContent(text);
  });

  it("shows outcomes without a reason", async () => {
    const { sync } = await openDashboard({ status: withSync({ last: result("too_soon") }) });
    expect(within(sync).getByTestId("sync-last")).toHaveTextContent(
      "Zu kurz nach dem letzten Snapshot; der Wächter legt höchstens alle 30 Minuten einen an.",
    );
    expect(within(sync).queryByTestId("sync-reason")).toBeNull();
  });

  it("explains it in English", async () => {
    const { user, sync } = await openDashboard({
      status: withSync({ last: result("target_refused", "space_low"), report: REPORT }),
    });
    await user.click(screen.getByRole("button", { name: "Englisch" }));
    expect(within(sync).getByTestId("sync-reason")).toHaveTextContent(
      "Reason: Not enough space on Unraid Groß (reserve from mirror.conf).",
    );
    expect(within(sync).getByRole("button", { name: "Dry run only" })).toBeEnabled();
    expect(within(sync).getByTestId("sync-share")).toHaveTextContent("present · 5 snapshots");
  });
});

describe("ZFS cache on the target", () => {
  it("warns in the tile when no limit is set", async () => {
    installFakeApi({
      authenticated: true,
      status: { ...STATUS, notices: ["target_arc_unlimited"] },
    });
    render(<App />);
    const target = await screen.findByTestId("tile-target");
    expect(
      await within(target).findByText(
        "Auf Unraid Groß ist keine Grenze für den ZFS-Cache eingetragen. Trage sie in Unraid unter Settings → Disk Settings ein.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [{ arc_max: 3.2 * GIB, c_max: 3.2 * GIB }, "zfs_arc_max 3,2 GiB · wirksam (c_max) 3,2 GiB"],
    [
      { arc_max: 0, c_max: 15 * GIB },
      "zfs_arc_max nicht eingetragen (Settings → Disk Settings) · wirksam (c_max) 15 GiB",
    ],
    [{ arc_max: null, c_max: null }, "zfs_arc_max ? · wirksam (c_max) ?"],
    [null, "unbekannt (Server aus oder noch keine Statuszeile)"],
  ])("shows %o in the settings", async (zfs, text) => {
    const { user } = await openDashboard({ status: { ...STATUS, target_zfs: zfs } });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("target-arc")).toHaveTextContent(text);
  });
});

describe("settings", () => {
  it("shows the three levels and the mirrors of the last check", async () => {
    const status = withSync(
      {
        levels: {
          configured: ["twinraidtest", "dokumente"],
          source: ["twinraidtest"],
          target: ["twinraidtest", "alt"],
          target_checked_at: CHECK.at,
        },
      },
      {
        source_guard: {
          ...GUARD_ACTIVE,
          details: { ...GUARD_DETAILS, target_check: CHECK },
        },
      },
    );
    const { user } = await openDashboard({ status });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const card = await screen.findByTestId("settings-sync");
    const rows = within(within(card).getByTestId("sync-levels")).getAllByRole("row");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Sharetwinraid.yamlUnraid Klein (source-guard.conf)Unraid Groß (mirror.conf)",
      "twinraidtestjajaja",
      "dokumentejaneinnein",
      "altneinneinja",
    ]);
    expect(within(card).getByTestId("sync-check")).toHaveTextContent("beantwortet");
    expect(within(card).getByTestId("sync-check-summary")).toHaveTextContent(
      "600 GiB von 860 GiB frei · Reserve 15 % · Aufbewahrung 30 Tage · Delegation ok · Empfang idle · Klone 0",
    );
    expect(within(card).getByTestId("sync-mirrors")).toHaveTextContent("twinraidtestvorhanden5");
  });

  it("explains that the target is known only after a dry run", async () => {
    const { user } = await openDashboard();
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const card = await screen.findByTestId("settings-sync");
    expect(within(card).getByTestId("sync-no-check")).toHaveTextContent(
      "Noch keine Prüfung von Unraid Groß; sie kommt mit dem ersten Probelauf.",
    );
    const rows = within(within(card).getByTestId("sync-levels")).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("twinraidtestjaja?");
  });

  it("marks a failed check", async () => {
    const check = { ...CHECK, result: "auth_failed" as const, summary: null, mirrors: [] };
    const { user } = await openDashboard({
      status: {
        ...STATUS,
        source_guard: { ...GUARD_ACTIVE, details: { ...GUARD_DETAILS, target_check: check } },
      },
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const line = await screen.findByTestId("sync-check");
    expect(line).toHaveTextContent(
      "Unraid Groß lehnt den Key „sync“ ab (Zeile in authorized_keys prüfen)",
    );
    expect(line).toHaveClass("text-destructive");
  });

  it("lists the sync switches", async () => {
    const { user } = await openDashboard({
      mode: "real",
      info: {
        ...INFO,
        mode: "real",
        actions: { wake: false, vm: false, shutdown: false, sync_dry_run: true, sync: false },
      },
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("action-group-sync_dry_run")).toHaveTextContent(
      "freigeschaltet",
    );
    expect(screen.getByTestId("action-group-sync")).toHaveTextContent("gesperrt");
  });
});
