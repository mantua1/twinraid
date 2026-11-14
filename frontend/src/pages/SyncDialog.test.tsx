// Kachel Sync ab v0.5.3: Shares-Zeile, Dialog vor dem Sync, Zeiten, Snapshots (Konzept 4.1).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GuardDetails, ResourceState, StatusSnapshot, SyncStatus } from "@/api/types";
import {
  actionsSnapshot,
  CHECK,
  dataset,
  GUARD_ACTIVE,
  GUARD_DETAILS,
  installFakeApi,
  latestStream,
  REPORT,
  SHARE_REPORT,
  STATUS,
  SYNC_IDLE,
} from "@/test/fakeApi";

import { App } from "../App";

afterEach(() => {
  vi.useRealTimers();
});

function clockAt(iso: string) {
  // Nur Date: Timer und Promises bleiben echt, sonst hängen userEvent und waitFor.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(iso));
}

function status(
  target: ResourceState,
  sync: Partial<SyncStatus> = {},
  details: Partial<GuardDetails> = {},
): StatusSnapshot {
  return {
    ...STATUS,
    target: { state: target, reason: null },
    source_guard: { ...GUARD_ACTIVE, details: { ...GUARD_DETAILS, ...details } },
    sync: { ...SYNC_IDLE, ...sync },
  };
}

function emit(next: StatusSnapshot) {
  act(() => {
    latestStream().emit("status", next);
  });
}

async function openDashboard(initial: StatusSnapshot) {
  const server = installFakeApi({ authenticated: true, status: initial });
  const user = userEvent.setup();
  render(<App />);
  const tile = await screen.findByTestId("tile-sync");
  await screen.findByTestId("checked-at");
  return { server, user, tile };
}

const START = { name: "Jetzt synchronisieren" };

describe("shares line", () => {
  it("names the shares and the mirror, also when the buttons are locked", async () => {
    const { tile } = await openDashboard({
      ...status("offline"),
      actions: actionsSnapshot({ sync_start: "source_guard_inactive" }),
    });
    expect(within(tile).getByTestId("sync-shares")).toHaveTextContent(
      "Shares: twinraidtest → Spiegel auf Unraid Groß",
    );
  });
});

describe("dialog before the sync", () => {
  it("names shares, containers and the wake, and sends only after the confirmation", async () => {
    const { server, user, tile } = await openDashboard(
      status("offline", { preview: { containers: ["alpha", "beta"], wake: true } }),
    );
    await user.click(within(tile).getByRole("button", START));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Sync starten?")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Gespiegelt werden twinraidtest nach Unraid Groß.");
    expect(dialog).toHaveTextContent(
      "Voraussichtlich werden 2 Container kurz angehalten: alpha, beta.",
    );
    expect(dialog).toHaveTextContent("Unraid Groß ist aus und wird geweckt; danach bleibt es an.");
    expect(server.actions).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(server.actions).toEqual([]);

    await user.click(within(tile).getByRole("button", START));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Sync starten" }),
    );
    expect(server.actions).toEqual(["sync_start"]);
  });

  it("uses the singular for one container", async () => {
    const { user, tile } = await openDashboard(
      status("online", { preview: { containers: ["alpha"], wake: false } }),
    );
    await user.click(within(tile).getByRole("button", START));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Voraussichtlich wird 1 Container kurz angehalten: alpha.",
    );
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Unraid Groß läuft schon und wird nicht geweckt.",
    );
  });

  it("says so when no allowed container runs", async () => {
    const { user, tile } = await openDashboard(
      status("online", { preview: { containers: [], wake: false } }),
    );
    await user.click(within(tile).getByRole("button", START));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Kein freigegebener Container läuft; es wird keiner angehalten.",
    );
  });

  it("says that no container is stopped with stop_containers: false", async () => {
    const { user, tile } = await openDashboard(
      status("online", { preview: { containers: null, wake: false } }),
    );
    await user.click(within(tile).getByRole("button", START));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Es werden keine Container angehalten; der Snapshot ist für laufende Container nur absturzkonsistent.",
    );
  });

  it.each(["starting", "stopping", "unknown"] as const)(
    "does not claim that the target is not woken while it is %s",
    async (state) => {
      const { user, tile } = await openDashboard(status("online"));
      await user.click(within(tile).getByRole("button", START));
      emit(status(state));
      const dialog = screen.getByRole("dialog");
      expect(dialog).toHaveTextContent("ist gerade weder aus noch an");
      expect(dialog).toHaveTextContent("steht erst beim Start fest");
      expect(dialog).not.toHaveTextContent("nicht geweckt");
    },
  );

  it("follows the live status while it is open", async () => {
    const { user, tile } = await openDashboard(
      status("offline", { preview: { containers: ["alpha"], wake: true } }),
    );
    await user.click(within(tile).getByRole("button", START));
    expect(screen.getByRole("dialog")).toHaveTextContent("wird geweckt");
    emit(status("online", { preview: { containers: ["alpha", "beta"], wake: false } }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("läuft schon und wird nicht geweckt");
    expect(dialog).toHaveTextContent("2 Container");
  });

  it("does not ask before the dry run", async () => {
    const { server, user, tile } = await openDashboard(status("online"));
    await user.click(within(tile).getByRole("button", { name: "Nur Probelauf" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(server.actions).toEqual(["sync_dry_run"]);
  });

  it("speaks English", async () => {
    const { user, tile } = await openDashboard(
      status("offline", { preview: { containers: ["alpha"], wake: true } }),
    );
    await user.click(screen.getByRole("button", { name: "Englisch" }));
    await user.click(within(tile).getByRole("button", { name: "Sync now" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("1 container will probably be stopped briefly: alpha.");
    expect(dialog).toHaveTextContent("is off and will be woken; it stays on afterwards.");
  });
});

describe("pause and snapshot interval", () => {
  it("names the pause at the rounded minute and keeps the line until that minute", async () => {
    clockAt("2026-10-13T12:00:00Z");
    const { tile } = await openDashboard({
      ...status("online", {}, { run_allowed_at: "2026-10-13T12:00:10Z" }),
      actions: actionsSnapshot({ sync_start: "source_guard_too_soon" }),
    });
    // Wie beim Abstand zwischen Snapshots: hh:mm, aufgerundet (Konzept 3.6).
    const time = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date("2026-10-13T12:01:00Z"),
    );
    expect(within(tile).getByTestId("next-sync")).toHaveTextContent(
      `Nächster Sync frühestens um ${time}.`,
    );
    expect(within(tile).getByTestId("action-hint")).toHaveTextContent(`frühestens um ${time}`);
    // run_allowed_at ist erreicht, die Minute noch nicht: Die Zeile bleibt, wie die Sperre.
    vi.setSystemTime(new Date("2026-10-13T12:00:20Z"));
    await new Promise((resolve) => setTimeout(resolve, 1300));
    expect(within(tile).getByTestId("next-sync")).toHaveTextContent(
      `Nächster Sync frühestens um ${time}.`,
    );
    vi.setSystemTime(new Date("2026-10-13T12:01:00Z"));
    await waitFor(
      () => {
        expect(within(tile).queryByTestId("next-sync")).toBeNull();
      },
      { timeout: 3000 },
    );
    expect(within(tile).queryByText(/frühestens um/)).toBeNull();
  });

  it("does not show the pause when the sync stops no containers", async () => {
    clockAt("2026-10-13T12:00:00Z");
    const { tile } = await openDashboard(
      status(
        "online",
        { preview: { containers: null, wake: false } },
        { run_allowed_at: "2026-10-13T12:29:10Z" },
      ),
    );
    expect(within(tile).queryByTestId("next-sync")).toBeNull();
  });

  it("names the next snapshot at the rounded minute, at both buttons, and drops it in time", async () => {
    clockAt("2026-10-13T12:00:00Z");
    const { tile } = await openDashboard({
      ...status("online", { snapshot_allowed_at: "2026-10-13T12:01:00Z" }),
      actions: actionsSnapshot({
        sync_dry_run: "source_guard_snapshot_too_soon",
        sync_start: "source_guard_snapshot_too_soon",
      }),
    });
    const time = new Intl.DateTimeFormat("de", { timeStyle: "short" }).format(
      new Date("2026-10-13T12:01:00Z"),
    );
    expect(within(tile).getByTestId("next-snapshot")).toHaveTextContent(
      `Nächster Snapshot frühestens um ${time}.`,
    );
    expect(within(tile).getByTestId("action-hint")).toHaveTextContent(
      `Nur Probelauf, Jetzt synchronisieren: seit dem letzten Snapshot auf Unraid Klein ist der Mindestabstand noch nicht vergangen; frühestens um ${time}`,
    );
    vi.setSystemTime(new Date("2026-10-13T12:01:00Z"));
    await waitFor(
      () => {
        expect(within(tile).queryByTestId("next-snapshot")).toBeNull();
      },
      { timeout: 3000 },
    );
  });

  it("shows both lines when both limits apply", async () => {
    clockAt("2026-10-13T12:00:00Z");
    const { tile } = await openDashboard(
      status(
        "online",
        { snapshot_allowed_at: "2026-10-13T12:05:00Z" },
        { run_allowed_at: "2026-10-13T12:20:00Z" },
      ),
    );
    expect(within(tile).getByTestId("next-sync")).toBeInTheDocument();
    expect(within(tile).getByTestId("next-snapshot")).toBeInTheDocument();
  });
});

describe("snapshots of both sides", () => {
  const withNewest = {
    job: {
      created_at: "2026-10-13T11:55:00Z",
      datasets: [{ name: "twinraidtest" }],
    },
    zfs: {
      ...GUARD_DETAILS.zfs,
      datasets: [
        dataset("twinraidtest", true, {
          snapshots: 10,
          oldest_snapshot_at: "2026-10-07T11:00:00Z",
          base_created_at: "2026-10-13T11:00:00Z",
          bookmark: true,
        }),
      ],
    },
    target_check: CHECK,
  } satisfies Partial<GuardDetails>;

  it("lists the numbers of Klein and Groß for each share", async () => {
    const { user, tile } = await openDashboard(status("online", {}, withNewest));
    const share = within(tile).getByTestId("snapshots-share");
    await user.click(within(share).getByText("Snapshots von twinraidtest"));
    const source = within(share).getByTestId("snapshots-source");
    expect(source).toHaveTextContent("Auf Unraid Klein");
    expect(source).toHaveTextContent("10 Snapshots");
    expect(source).toHaveTextContent("neuester:");
    expect(source).toHaveTextContent("ältester:");
    expect(source).toHaveTextContent("(gehalten)");
    expect(source).toHaveTextContent("Lesezeichen vorhanden");
    expect(source).toHaveTextContent("Aufbewahrung 7 Tage");
    const target = within(share).getByTestId("snapshots-target");
    expect(target).toHaveTextContent("Auf Unraid Groß");
    expect(target).toHaveTextContent("5 Snapshots");
    expect(target).toHaveTextContent("neuester empfangen:");
    expect(target).toHaveTextContent("erstellt:");
    expect(target).toHaveTextContent("Aufbewahrung 30 Tage");
    expect(target).toHaveTextContent("Stand: Prüfung vom");
    expect(within(tile).getByTestId("sync-snapshots")).toHaveTextContent(
      "docs/WIEDERHERSTELLUNG.md",
    );
  });

  it("says unknown without a check and for a missing basis", async () => {
    const { tile } = await openDashboard(
      status(
        "online",
        {},
        {
          zfs: {
            ...GUARD_DETAILS.zfs,
            datasets: [dataset("twinraidtest", true, { snapshots: 3 })],
          },
        },
      ),
    );
    const share = within(tile).getByTestId("snapshots-share");
    expect(within(share).getByTestId("snapshots-target")).toHaveTextContent(
      "unbekannt (noch kein Probelauf oder Sync)",
    );
    expect(within(share).getByTestId("snapshots-source")).toHaveTextContent("Basis: keine");
    expect(within(share).getByTestId("snapshots-source")).toHaveTextContent("kein Lesezeichen");
  });

  it.each([
    ["absent", "ok", "noch kein Spiegel"],
    ["foreign", "ok", "fremdes Dataset"],
    ["mirror", "mismatch", "gesperrt"],
  ] as const)("shows the mirror state %s with props %s", async (state, props, text) => {
    const mirror = {
      name: "twinraidtest",
      count: 5,
      newest_created: null,
      newest_received: null,
      oldest_created: null,
      state,
      props,
    };
    const { tile } = await openDashboard(
      status("online", {}, { target_check: { ...CHECK, mirrors: [mirror] } }),
    );
    const target = within(tile).getByTestId("snapshots-target");
    expect(target).toHaveTextContent(text);
    expect(target).not.toHaveTextContent("neuester empfangen");
  });

  it("shows the source side as unknown without a fresh guard status", async () => {
    const { tile } = await openDashboard({
      ...status("online"),
      source_guard: { ...GUARD_ACTIVE, state: "inactive", details: null },
    });
    expect(within(tile).getByTestId("snapshots-source")).toHaveTextContent("unbekannt");
  });
});

describe("uncounted runs", () => {
  it("replaces the counts of a first run", async () => {
    const full = { ...SHARE_REPORT, mode: "full", base_type: null } as const;
    const { tile } = await openDashboard(
      status("online", {
        report: {
          ...REPORT,
          shares: [{ ...full, deleted: null, modified: null, added: null, renamed: null }],
        },
      }),
    );
    expect(within(tile).getByTestId("sync-uncounted")).toHaveTextContent(
      "nicht gezählt (erster Lauf)",
    );
  });

  it("names the bookmark and keeps real counts", async () => {
    const bookmark = {
      ...SHARE_REPORT,
      base_type: "bookmark",
      deleted: null,
      modified: null,
      added: null,
      renamed: null,
    } as const;
    const { tile } = await openDashboard(
      status("online", {
        report: { ...REPORT, shares: [bookmark, { ...SHARE_REPORT, name: "zwei" }] },
      }),
    );
    expect(within(tile).getAllByTestId("sync-uncounted")).toHaveLength(1);
    expect(within(tile).getByTestId("sync-uncounted")).toHaveTextContent(
      "nicht gezählt (vom Lesezeichen)",
    );
    expect(within(tile).getAllByTestId("sync-share")[1]).toHaveTextContent("12");
  });

  it("marks the history entry of a first run", async () => {
    const { user, tile } = await openDashboard(
      status("online", {
        history: [
          {
            id: "1",
            kind: "sync",
            trigger: "dashboard",
            started_at: "2026-10-13T12:00:00Z",
            finished_at: "2026-10-13T12:02:00Z",
            result: "success",
            reason: null,
            woke_target: false,
            containers_stopped: 0,
            adopted: false,
            shares: [
              {
                name: "twinraidtest",
                mode: "full",
                base_type: null,
                deleted: null,
                modified: null,
                added: null,
                renamed: null,
                size: 1,
                sent: 1,
                result: "ok",
              },
            ],
          },
        ],
      }),
    );
    await user.click(within(tile).getByText("Letzter Lauf"));
    expect(within(tile).getByTestId("sync-history-uncounted")).toHaveTextContent(
      "twinraidtest: nicht gezählt (erster Lauf)",
    );
  });
});
