// Kachel Sync und Einstellungen zum Probelauf (Konzept 4.1, 4.3 und D5, ab v0.5.1).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { describe, expect, it } from "vitest";

import type {
  StatusSnapshot,
  SyncHistoryEntry,
  SyncResult,
  SyncRun,
  SyncStatus,
} from "@/api/types";
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
  syncRun,
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
  // Die Knöpfe stehen schon vor dem ersten Status da; erst die Zeit der Prüfung zeigt, dass
  // er angekommen ist. Sonst prüften die Tests unter Last die leere Kachel.
  if (server.status?.checked_at != null) {
    await screen.findByTestId("checked-at");
  }
  return { server, user, sync };
}

function emit(status: StatusSnapshot) {
  act(() => {
    latestStream().emit("status", status);
  });
}

describe("buttons", () => {
  it("starts a dry run; the sync is available from v0.5.2", async () => {
    const { server, user, sync } = await openDashboard();
    const dryRun = within(sync).getByRole("button", { name: "Nur Probelauf" });
    const start = within(sync).getByRole("button", { name: "Jetzt synchronisieren" });
    expect(start).toBeEnabled();
    expect(within(sync).queryByTestId("action-hint")).not.toBeInTheDocument();
    expect(within(sync).getByTestId("sync-schedule")).toHaveTextContent(
      "Zeitplan aus (ab Phase 6).",
    );
    expect(within(sync).getByText("Noch kein Lauf.")).toBeInTheDocument();
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
  const running = syncRun();

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
      "Unraid Groß: 1.000 GiB von 2 TiB frei, Reserve 15 %.",
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

describe("running sync", () => {
  const sync = (overrides: Partial<SyncRun> = {}) =>
    withSync(
      { running: syncRun({ kind: "sync", ...overrides }), report: REPORT },
      { job_running: true, actions: actionsSnapshot({ sync_dry_run: "busy", sync_start: "busy" }) },
    );
  const confirmation = {
    reasons: ["deletions" as const, "shrunk" as const],
    deletions: 1500,
    max_deletions: 1000,
    deadline: "2026-10-13T12:31:00Z",
  };
  const shrunkReport = {
    ...REPORT,
    awaiting_confirm: true,
    confirm_reasons: ["deletions" as const, "shrunk" as const],
    shares: [{ ...SHARE_REPORT, deleted: 1500, referenced: 300 * 1024 ** 2 }],
  };

  it("names the stopped containers", async () => {
    const { sync: tile } = await openDashboard({
      status: sync({ step: "stopping_containers", containers_stopped: 1 }),
    });
    expect(within(tile).getByTestId("sync-running")).toHaveTextContent(
      "Sync läuft: Container auf Unraid Klein werden angehalten …",
    );
    expect(within(tile).getByTestId("sync-containers")).toHaveTextContent(
      "1 Container auf Unraid Klein angehalten.",
    );
    expect(within(tile).getByRole("button", { name: "Jetzt synchronisieren" })).toBeDisabled();
    // Sind die Container wieder gestartet, entfällt die Zeile.
    emit(sync({ step: "preparing", containers_stopped: 1 }));
    expect(within(tile).queryByTestId("sync-containers")).toBeNull();
  });

  it("asks before cancelling a sync", async () => {
    const { server, user, sync: tile } = await openDashboard({ status: sync({ step: "sending" }) });
    await user.click(within(tile).getByRole("button", { name: "Abbrechen" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Sync abbrechen?");
    expect(dialog).toHaveTextContent(
      "Die Share, die gerade gesendet wird, und alle noch nicht gesendeten behalten auf Unraid Groß den bisherigen Stand",
    );
    expect(dialog).toHaveTextContent("Schon gesendete Shares sind auf Unraid Groß aktuell.");
    await user.click(within(dialog).getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(server.cancelled).toEqual([]);
    await user.click(within(tile).getByRole("button", { name: "Abbrechen" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Sync abbrechen" }),
    );
    await waitFor(() => {
      expect(server.cancelled).toEqual([JOB_ID]);
    });
    // Bis der Live-Status den Abbruch zeigt, bleibt der Button gesperrt.
    expect(within(tile).getByRole("button", { name: "Abbrechen" })).toBeDisabled();
  });

  it.each([
    [true, "Wird abgebrochen … (wartet auf den Container)"],
    [false, "Wird abgebrochen …"],
  ])("shows a cancel that waits for the container: %s", async (waiting, text) => {
    const { sync: tile } = await openDashboard({
      status: sync({
        step: "stopping_containers",
        cancelling: true,
        waiting_for_container: waiting,
      }),
    });
    expect(within(tile).getByTestId("sync-running")).toHaveTextContent(text);
    expect(within(tile).getByTestId("sync-running")).not.toHaveTextContent("Sync läuft");
    expect(within(tile).getByRole("button", { name: "Abbrechen" })).toBeDisabled();
  });

  it("locks confirm and decline while cancelling", async () => {
    const { sync: tile } = await openDashboard({
      status: sync({ step: "awaiting_confirm", confirmation, cancelling: true }),
    });
    expect(within(tile).getByRole("button", { name: "Freigeben und senden" })).toBeDisabled();
    expect(within(tile).getByRole("button", { name: "Ablehnen" })).toBeDisabled();
  });

  it("asks for the deletions and confirms them after a dialog", async () => {
    const {
      server,
      user,
      sync: tile,
    } = await openDashboard({
      status: withSync(
        {
          running: syncRun({ kind: "sync", step: "awaiting_confirm", confirmation }),
          report: shrunkReport,
        },
        { job_running: true },
      ),
    });
    const box = within(tile).getByTestId("sync-confirmation");
    expect(box).toHaveTextContent("Löschungen freigeben?");
    expect(box).toHaveTextContent(
      "1500 Löschungen, mehr als max_deletions (1000) in source-guard.conf.",
    );
    expect(box).toHaveTextContent(
      "Auf weniger als die Hälfte geschrumpft (neu / Basis): twinraidtest: 300 MiB / 2,1 GiB.",
    );
    expect(box).toHaveTextContent(
      "Freigegebene Löschungen bleiben in den älteren Snapshots auf Unraid Groß erhalten.",
    );
    const deadline = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date(confirmation.deadline),
    );
    expect(within(tile).getByTestId("sync-confirmation-deadline")).toHaveTextContent(
      `Ohne Antwort schließt der Wächter den Job um ${deadline}; dann wird nichts gesendet.`,
    );
    // Ablehnen ersetzt hier das allgemeine Abbrechen.
    expect(within(tile).queryByRole("button", { name: "Abbrechen" })).toBeNull();
    await user.click(within(tile).getByRole("button", { name: "Freigeben und senden" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Löschungen freigeben und senden?");
    await user.click(within(dialog).getByRole("button", { name: "Freigeben und senden" }));
    await waitFor(() => {
      expect(server.confirmed).toEqual([JOB_ID]);
    });
    const call = server.calls.find(
      (entry) => entry.url === `/api/sync/${JOB_ID}/confirm-deletions`,
    );
    expect(call?.method).toBe("POST");
    expect(call?.headers.get("X-CSRF-Token")).toBe(CSRF);
    expect(within(tile).getByRole("button", { name: "Freigeben und senden" })).toBeDisabled();
    expect(within(tile).getByRole("button", { name: "Ablehnen" })).toBeDisabled();
    // Sobald der Sync sendet, ist Abbrechen wieder möglich.
    emit(withSync({ running: syncRun({ kind: "sync", step: "sending" }), report: shrunkReport }));
    expect(within(tile).getByRole("button", { name: "Abbrechen" })).toBeEnabled();
  });

  it("declines the deletions without a dialog", async () => {
    const {
      server,
      user,
      sync: tile,
    } = await openDashboard({
      status: sync({ step: "awaiting_confirm", confirmation }),
    });
    await user.click(within(tile).getByRole("button", { name: "Ablehnen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(server.cancelled).toEqual([JOB_ID]);
    });
    expect(server.confirmed).toEqual([]);
  });

  it("says so when confirming did not work", async () => {
    const { user, sync: tile } = await openDashboard({
      status: sync({ step: "awaiting_confirm", confirmation }),
      confirmAccepted: false,
    });
    await user.click(within(tile).getByRole("button", { name: "Freigeben und senden" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Freigeben und senden" }),
    );
    expect(await within(tile).findByRole("alert")).toHaveTextContent(
      "Freigeben hat nicht geklappt. Bitte erneut versuchen.",
    );
    expect(within(tile).getByRole("button", { name: "Freigeben und senden" })).toBeEnabled();
  });

  it("shows the progress while sending", async () => {
    const progress = {
      share: "twinraidtest",
      sent: 150 * 1024 ** 2,
      total: 350 * 1024 ** 2,
      percent: 42,
      rate: 25 * 1024 ** 2,
      remaining_seconds: 125,
      finish_at: "2026-10-13T12:05:00Z",
    };
    const { sync: tile } = await openDashboard({ status: sync({ step: "sending", progress }) });
    expect(within(tile).getByTestId("sync-running")).toHaveTextContent(
      "Sync läuft: Sendet nach Unraid Groß …",
    );
    const box = within(tile).getByTestId("sync-progress");
    expect(box.querySelector("progress")).toHaveAttribute("value", "42");
    expect(box).toHaveTextContent("42 % · 150 MiB von 350 MiB · Share twinraidtest");
    const time = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date(progress.finish_at),
    );
    expect(within(tile).getByTestId("sync-eta")).toHaveTextContent(
      `25 MiB/s · Restzeit 2 min 5 s · fertig gegen ${time}`,
    );
    emit(sync({ step: "sending", progress: { ...progress, rate: null, remaining_seconds: null } }));
    expect(within(tile).getByTestId("sync-eta")).toHaveTextContent("Restzeit wird berechnet …");
  });

  it("names an adopted transfer", async () => {
    const { sync: tile } = await openDashboard({
      status: sync({ step: "sending", adopted: true }),
    });
    expect(within(tile).getByTestId("sync-adopted")).toHaveTextContent(
      "Übertragung läuft weiter (vor dem Neustart begonnen).",
    );
  });

  it("explains the cancel in English", async () => {
    const { user, sync: tile } = await openDashboard({
      status: sync({ step: "starting_containers", cancelling: true, waiting_for_container: true }),
    });
    await user.click(screen.getByRole("button", { name: "Englisch" }));
    expect(within(tile).getByTestId("sync-running")).toHaveTextContent(
      "Cancelling … (waiting for the container)",
    );
  });
});

describe("finished sync", () => {
  const entry = (overrides: Partial<SyncHistoryEntry> = {}): SyncHistoryEntry => ({
    id: "a1",
    kind: "sync",
    trigger: "dashboard",
    started_at: "2026-10-13T11:59:00Z",
    finished_at: "2026-10-13T12:01:05Z",
    result: "success",
    reason: null,
    woke_target: false,
    containers_stopped: 1,
    adopted: false,
    shares: [
      {
        name: "twinraidtest",
        mode: "incremental",
        base_type: "snapshot",
        deleted: 12,
        modified: 34,
        added: 56,
        renamed: 2,
        size: 350 * 1024 ** 2,
        sent: 350 * 1024 ** 2,
        result: "ok",
      },
    ],
    ...overrides,
  });
  const last = { ...result("success"), kind: "sync" as const };

  it("shows the result with duration, data and the sent column", async () => {
    const report = {
      ...REPORT,
      kind: "sync" as const,
      shares: [{ ...SHARE_REPORT, sent: 350 * 1024 ** 2, result: "ok" as const }],
    };
    const { sync: tile } = await openDashboard({
      status: withSync({ last, report, history: [entry()] }),
    });
    const lastLine = within(tile).getByTestId("sync-last");
    expect(lastLine).toHaveTextContent("Letzter Sync");
    expect(lastLine).toHaveTextContent("erfolgreich");
    expect(lastLine.querySelector("p")).not.toHaveClass("text-destructive");
    expect(within(tile).getByTestId("sync-last-details")).toHaveTextContent(
      "Dauer 2 min 5 s · 350 MiB gesendet · 1 Container angehalten",
    );
    expect(within(tile).getByTestId("sync-report")).toHaveTextContent("Ergebnis des Syncs vom");
    expect(within(tile).getByTestId("sync-share-sent")).toHaveTextContent("350 MiBangekommen");
    // Der Bericht eines Syncs sagt nicht „Senden wäre möglich“.
    expect(within(tile).queryByTestId("sync-send-ready")).toBeNull();
  });

  it("lists the history, newest first, with reasons", async () => {
    const history = [
      entry({ id: "a3", result: "send_failed", reason: "unreachable", woke_target: true }),
      entry({ id: "a2", kind: "dry_run", result: "dry_run_success", containers_stopped: 0 }),
      entry({ id: "a1", adopted: true }),
    ];
    const { sync: tile } = await openDashboard({ status: withSync({ history }) });
    const box = within(tile).getByTestId("sync-history");
    expect(box).toHaveTextContent("Letzte 3 Läufe");
    const items = within(box).getAllByTestId("sync-history-entry");
    expect(items[0]).toHaveTextContent(
      "Sync · Dauer 2 min 5 s · 350 MiB gesendet · 1 Container angehalten · hat Unraid Groß geweckt",
    );
    expect(items[0]).toHaveTextContent(
      "Das Senden ist gescheitert. Shares, die nicht angekommen sind (gescheitert oder übersprungen), behalten auf dem Zielserver den bisherigen Stand; erfolgreich gesendete Shares sind dort aktuell. Grund: SSH auf Unraid Groß nicht erreichbar",
    );
    expect(items[0]?.querySelectorAll("p")[1]).toHaveClass("text-destructive");
    expect(items[1]).toHaveTextContent("Probelauf · Dauer 2 min 5 s");
    expect(items[1]).not.toHaveTextContent("gesendet");
    expect(items[2]).toHaveTextContent("nach einem Neustart übernommen");
  });

  it("has no history before the first run", async () => {
    const { sync: tile } = await openDashboard();
    expect(within(tile).queryByTestId("sync-history")).toBeNull();
  });

  it.each([
    [
      result("source_guard_refused", "no_run"),
      "Grund: der Totmannschalter des Wächters hat den Lauf beendet, weil TwinRaid sich nicht rechtzeitig gemeldet hat; die Container laufen wieder",
    ],
    [
      result("source_guard_refused", "stop_failed"),
      "Grund: docker stop ist für einen Container fehlgeschlagen",
    ],
    [
      result("target_refused", "received_properties"),
      "Grund: der Strom brachte Eigenschaften mit, der Spiegel ist gesperrt (received-properties); Schritte in docs/WIEDERHERSTELLUNG.md",
    ],
    [
      result("send_failed", "send_error"),
      "Grund: zfs send ist auf Unraid Klein gescheitert; Einzelheiten im Syslog (twinraid-source-guard)",
    ],
  ])("names the reason of the sync result %o", async (outcome, text) => {
    const { sync: tile } = await openDashboard({
      status: withSync({ last: { ...outcome, kind: "sync" } }),
    });
    expect(within(tile).getByTestId("sync-reason")).toHaveTextContent(text);
  });

  it("names the pause of the source guard at the sync button", async () => {
    const { sync: tile } = await openDashboard({
      status: {
        ...STATUS,
        source_guard: {
          ...GUARD_ACTIVE,
          details: { ...GUARD_DETAILS, run_allowed_at: "2026-10-13T12:29:10Z" },
        },
        actions: actionsSnapshot({ sync_start: "source_guard_too_soon" }),
      },
    });
    const time = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date("2026-10-13T12:30:00Z"),
    );
    expect(within(tile).getByTestId("action-hint")).toHaveTextContent(
      `Jetzt synchronisieren: der Wächter auf Unraid Klein hält die Pause zwischen zwei Läufen ein; frühestens um ${time}`,
    );
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
    [{ arc_max: 4 * GIB, c_max: 4 * GIB }, "zfs_arc_max 4 GiB · wirksam (c_max) 4 GiB"],
    [
      { arc_max: 0, c_max: 16 * GIB },
      "zfs_arc_max nicht eingetragen (Settings → Disk Settings) · wirksam (c_max) 16 GiB",
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
      "1.000 GiB von 2 TiB frei · Reserve 15 % · Aufbewahrung 30 Tage · Delegation ok · Empfang idle · Klone 0",
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
