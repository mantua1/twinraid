// Zeitplan und automatisches Herunterfahren in der Oberfläche (Konzept 4.1, 4.2, 4.8, ab v0.6.0).
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { describe, expect, it } from "vitest";

import type { ScheduleStatus, StatusSnapshot } from "@/api/types";
import { formatTerm } from "@/lib/schedule";
import {
  actionsSnapshot,
  AUTO_SHUTDOWN,
  installFakeApi,
  latestStream,
  SCHEDULE,
  STATUS,
  STATUS_VM_RUNNING,
  syncRun,
  type FakeServer,
} from "@/test/fakeApi";

import { App } from "../App";

function withSchedule(
  schedule: Partial<ScheduleStatus>,
  extra: Partial<StatusSnapshot> = {},
): StatusSnapshot {
  return { ...STATUS, ...extra, schedule: { ...SCHEDULE, ...schedule } };
}

async function openDashboard(overrides: Partial<FakeServer> = {}) {
  const server = installFakeApi({ authenticated: true, ...overrides });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByTestId("checked-at");
  return { server, user };
}

function emit(status: StatusSnapshot) {
  act(() => {
    latestStream().emit("status", status);
  });
}

const TERM = "2026-11-29T02:00:00Z";

describe("sync tile", () => {
  it("shows the next term and what happens to the target", async () => {
    await openDashboard({ status: withSchedule({}) });
    const sync = screen.getByTestId("tile-sync");
    expect(within(sync).getByTestId("schedule-next")).toHaveTextContent(
      `Nächster geplanter Lauf: ${formatTerm("de", TERM, "Europe/Berlin")}`,
    );
    expect(within(sync).getByTestId("schedule-wake")).toHaveTextContent(
      "Ist Unraid Groß aus, wird es geweckt und danach heruntergefahren.",
    );
  });

  it("names the time zone of the plan when it differs from the browser", async () => {
    // Eine Zone, in der der Testrechner sicher nicht steht.
    await openDashboard({ status: withSchedule({ timezone: "Pacific/Chatham" }) });
    expect(screen.getByTestId("schedule-next")).toHaveTextContent(
      `${formatTerm("de", TERM, "Pacific/Chatham")} (Pacific/Chatham)`,
    );
  });

  it.each([
    [{ wake: true, shutdown: false }, "wird es geweckt und bleibt danach an."],
    [{ wake: false, shutdown: false }, "Ist Unraid Groß aus, wird der Termin ausgelassen."],
  ])("explains %o", async (values, text) => {
    await openDashboard({ status: withSchedule(values) });
    expect(screen.getByTestId("schedule-wake")).toHaveTextContent(text);
  });

  it.each([
    [{ enabled: false }, "Zeitplan aus."],
    [{ permitted: false }, "Zeitplan: nicht freigeschaltet (actions.sync in twinraid.yaml)."],
  ])("shows %o", async (values, text) => {
    await openDashboard({ status: withSchedule(values) });
    const lines = screen.getByTestId("sync-schedule");
    expect(lines).toHaveTextContent(text);
    expect(within(lines).queryByTestId("schedule-next")).not.toBeInTheDocument();
  });

  it("shows a term waiting for a blocker", async () => {
    await openDashboard({
      status: withSchedule({
        waiting: {
          term: TERM,
          reason: "source_guard_too_soon",
          next_attempt_at: "2026-11-29T02:10:00Z",
          until: "2026-11-29T03:00:00Z",
        },
      }),
    });
    const waiting = screen.getByTestId("schedule-waiting");
    expect(waiting).toHaveTextContent(
      "Geplanter Lauf wartet: der Wächter auf Unraid Klein hält die Pause zwischen zwei Läufen ein, neuer Versuch um",
    );
    expect(waiting).toHaveTextContent("(längstens bis");
  });

  it("shows the last scheduled run, red unless it succeeded", async () => {
    await openDashboard({
      status: withSchedule({
        last: {
          scheduled_for: TERM,
          outcome: "schedule_skipped",
          reason: "source_guard_inactive",
          finished_at: "2026-11-29T03:00:05Z",
        },
      }),
    });
    const last = screen.getByTestId("schedule-last");
    expect(last).toHaveAttribute("data-outcome", "schedule_skipped");
    expect(last).toHaveTextContent("Letzter geplanter Lauf (Termin");
    expect(last).toHaveTextContent("Termin ausgelassen");
    expect(last).toHaveTextContent(
      "Grund: der Wächter auf Unraid Klein ist nicht aktiv oder ZFS ist dort nicht verfügbar",
    );
    expect(within(last).getByText(/Letzter geplanter Lauf/)).toHaveClass("text-destructive");
    emit(
      withSchedule({
        last: {
          scheduled_for: TERM,
          outcome: "success",
          reason: null,
          finished_at: "2026-11-29T02:10:00Z",
        },
      }),
    );
    expect(
      within(screen.getByTestId("schedule-last")).getByText(/Erfolgreich|erfolgreich/),
    ).not.toHaveClass("text-destructive");
  });

  it("marks a running scheduled run", async () => {
    await openDashboard({
      status: withSchedule(
        {},
        {
          sync: {
            ...STATUS.sync,
            running: syncRun({ kind: "sync", trigger: "schedule", scheduled_for: TERM }),
          },
        },
      ),
    });
    expect(screen.getByTestId("sync-running-scheduled")).toHaveTextContent(
      "Geplanter Lauf (Termin",
    );
  });
});

describe("target tile", () => {
  const planned: StatusSnapshot = {
    ...STATUS_VM_RUNNING,
    vm: { state: "offline", reason: null },
    auto_shutdown: AUTO_SHUTDOWN,
    actions: actionsSnapshot({
      wake_target: "target_online",
      shutdown_target: null,
      vm_stop: "vm_off",
      vm_force_stop: "vm_off",
      auto_shutdown_cancel: null,
    }),
  };

  it("shows the planned shutdown and keeps the target on without a dialog", async () => {
    const { server, user } = await openDashboard({ status: planned });
    const tile = screen.getByTestId("tile-target");
    expect(within(tile).getByTestId("auto-shutdown")).toHaveTextContent(
      "(nach dem geplanten Sync).",
    );
    expect(within(tile).getByTestId("auto-shutdown")).toHaveTextContent("Wird um");
    const keep = within(tile).getByRole("button", { name: "Unraid Groß anlassen" });
    await user.click(keep);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(server.actions).toEqual(["auto_shutdown_cancel"]);
  });

  it("names the missing condition instead of a time", async () => {
    await openDashboard({
      status: {
        ...planned,
        auto_shutdown: { kinds: ["after_play"], at: null, reason: "vm_running" },
      },
    });
    expect(screen.getByTestId("auto-shutdown")).toHaveTextContent(
      "Wird heruntergefahren (nach dem Spielen), sobald die VM aus ist.",
    );
  });

  it("shows neither line nor button without a mark", async () => {
    await openDashboard({ status: STATUS_VM_RUNNING });
    const tile = screen.getByTestId("tile-target");
    expect(within(tile).queryByTestId("auto-shutdown")).not.toBeInTheDocument();
    expect(within(tile).queryByRole("button", { name: /anlassen/ })).not.toBeInTheDocument();
  });

  it("names a failed automatic shutdown as such", async () => {
    await openDashboard({
      status: {
        ...STATUS_VM_RUNNING,
        actions: actionsSnapshot(
          {},
          {
            target: {
              running: null,
              last: {
                action: "shutdown_target",
                outcome: "other_vms_running",
                finished_at: "2026-11-29T02:30:00Z",
                trigger: "automatic",
              },
            },
          },
        ),
      },
    });
    expect(
      within(screen.getByTestId("tile-target")).getByTestId("action-result"),
    ).toHaveTextContent("Automatisches Herunterfahren");
  });
});

describe("settings", () => {
  it("shows the plan, the next three terms, the delays and a conflict", async () => {
    const { user } = await openDashboard({
      status: withSchedule({
        play_shutdown: true,
        conflicts: [{ limit: "min_run_interval_minutes", minutes: 1440 }],
        shortest_gap_minutes: 1380,
      }),
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    const card = await screen.findByTestId("settings-schedule");
    expect(within(card).getByTestId("schedule-state")).toHaveTextContent("eingeschaltet");
    expect(card).toHaveTextContent("So um 03:00 (Europe/Berlin)");
    expect(
      within(within(card).getByTestId("schedule-terms")).getAllByRole("listitem"),
    ).toHaveLength(3);
    expect(within(card).getByTestId("schedule-shutdown")).toHaveTextContent(
      "ja, 5 min nach dem Lauf, wenn er Unraid Groß geweckt hat",
    );
    expect(within(card).getByTestId("schedule-play")).toHaveTextContent("ja, 15 min");
    expect(card).toHaveTextContent("Kürzester Abstand zwischen zwei Terminen: 23 h.");
    expect(within(card).getByTestId("schedule-conflict")).toHaveTextContent(
      "min_run_interval_minutes (1440 min)",
    );
    expect(card).toHaveTextContent("Einen Termin auslassen geht nur in twinraid.yaml");
  });
});
