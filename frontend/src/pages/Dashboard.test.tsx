import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { describe, expect, it } from "vitest";

import type { StatusSnapshot } from "@/api/types";
import {
  actionsSnapshot,
  CSRF,
  INFO,
  installFakeApi,
  latestStream,
  STATUS,
  STATUS_VM_RUNNING,
  type FakeServer,
} from "@/test/fakeApi";

import { App } from "../App";

async function openDashboard(overrides: Partial<FakeServer> = {}) {
  const server = installFakeApi({ authenticated: true, ...overrides });
  const user = userEvent.setup();
  render(<App />);
  const target = await screen.findByTestId("tile-target");
  const vm = screen.getByTestId("tile-vm");
  return { server, user, target, vm };
}

function button(tile: HTMLElement, name: string): HTMLButtonElement {
  return within(tile).getByRole("button", { name });
}

function emit(status: StatusSnapshot) {
  act(() => {
    latestStream().emit("status", status);
  });
}

describe("actions", () => {
  it("offers waking and starting while the target is off", async () => {
    const { target, vm } = await openDashboard();
    await within(target).findByText("Nicht erreichbar");
    expect(button(target, "Wecken")).toBeEnabled();
    expect(button(target, "Herunterfahren")).toBeDisabled();
    expect(button(vm, "Starten")).toBeEnabled();
    expect(button(vm, "Herunterfahren")).toBeDisabled();
    expect(within(vm).queryByRole("button", { name: "Erzwingen" })).toBeNull();
    // Selbsterklärende Gründe (Server aus) brauchen keinen Hinweis.
    expect(screen.queryByTestId("action-hint")).toBeNull();
  });

  it("wakes the target with the CSRF token and without a dialog", async () => {
    const { server, user, target } = await openDashboard();
    await within(target).findByText("Nicht erreichbar");
    await user.click(button(target, "Wecken"));
    expect(server.actions).toEqual(["wake_target"]);
    const call = server.calls.find((c) => c.url === "/api/actions/wake_target");
    expect(call?.method).toBe("POST");
    expect(call?.headers.get("X-CSRF-Token")).toBe(CSRF);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the buttons locked until the live status follows", async () => {
    const { server, user, target } = await openDashboard();
    await within(target).findByText("Nicht erreichbar");
    await user.click(button(target, "Wecken"));
    expect(server.actions).toEqual(["wake_target"]);
    expect(button(target, "Wecken")).toBeDisabled();
    emit({ ...STATUS, actions: actionsSnapshot() });
    expect(button(target, "Wecken")).toBeEnabled();
  });

  it("asks to shut down the VM first when it is running", async () => {
    const { server, user, target } = await openDashboard({ status: STATUS_VM_RUNNING });
    await user.click(await within(target).findByRole("button", { name: "Herunterfahren" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("VM zuerst herunterfahren?")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Gaming-VM läuft");
    await user.click(within(dialog).getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(server.actions).toEqual([]);
    await user.click(button(target, "Herunterfahren"));
    await user.click(screen.getByRole("button", { name: "VM und Server herunterfahren" }));
    expect(server.actions).toEqual(["shutdown_target"]);
  });

  it("asks before shutting down the target without a VM", async () => {
    const status = { ...STATUS_VM_RUNNING, vm: { state: "offline" as const, reason: null } };
    const { server, user, target } = await openDashboard({ status });
    await user.click(await within(target).findByRole("button", { name: "Herunterfahren" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Unraid Groß herunterfahren?");
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Herunterfahren" }),
    );
    expect(server.actions).toEqual(["shutdown_target"]);
  });

  it("asks before shutting down the VM and names the waiting time", async () => {
    const { server, user, vm } = await openDashboard({ status: STATUS_VM_RUNNING });
    await user.click(await within(vm).findByRole("button", { name: "Herunterfahren" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Gaming-VM herunterfahren?");
    expect(dialog).toHaveTextContent("innerhalb von 3 Minuten");
    await user.click(within(dialog).getByRole("button", { name: "Herunterfahren" }));
    expect(server.actions).toEqual(["vm_stop"]);
  });

  it("offers force only after a shutdown attempt and only after the timeout", async () => {
    const pending: StatusSnapshot = {
      ...STATUS_VM_RUNNING,
      actions: { ...STATUS_VM_RUNNING.actions, force_available_at: "2026-10-13T12:03:00Z" },
    };
    const { server, user, vm } = await openDashboard({ status: pending });
    const force = await within(vm).findByRole("button", { name: "Erzwingen" });
    expect(force).toBeDisabled();
    expect(within(vm).getByTestId("action-hint")).toHaveTextContent(/^Erzwingen: möglich ab /);
    emit({
      ...pending,
      actions: actionsSnapshot(
        { ...STATUS_VM_RUNNING.actions.availability, vm_force_stop: null },
        { force_available_at: "2026-10-13T12:03:00Z" },
      ),
    });
    await user.click(button(vm, "Erzwingen"));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Gaming-VM sofort ausschalten?");
    expect(dialog).toHaveTextContent("Ziehen des Netzsteckers");
    await user.click(within(dialog).getByRole("button", { name: "Jetzt erzwingen" }));
    expect(server.actions).toEqual(["vm_force_stop"]);
  });

  it("explains actions that are not enabled", async () => {
    const status: StatusSnapshot = {
      ...STATUS,
      actions: actionsSnapshot({
        wake_target: "not_enabled",
        shutdown_target: "not_enabled",
        vm_start: "not_enabled",
        vm_stop: "not_enabled",
      }),
    };
    const { target, vm } = await openDashboard({
      status,
      mode: "real",
      info: { ...INFO, mode: "real", read_only: true },
    });
    expect(await within(target).findByTestId("action-hint")).toHaveTextContent(
      "Wecken, Herunterfahren: in twinraid.yaml unter actions nicht freigeschaltet",
    );
    expect(within(vm).getByTestId("action-hint")).toHaveTextContent(
      "Starten, Herunterfahren: in twinraid.yaml unter actions nicht freigeschaltet",
    );
    expect(screen.getByTestId("read-only-banner")).toHaveTextContent("unter actions");
  });

  it("shows the running step and then the result", async () => {
    const running: StatusSnapshot = {
      ...STATUS,
      target: { state: "starting", reason: null },
      vm: { state: "unknown", reason: "target_starting" },
      actions: actionsSnapshot(
        { wake_target: "busy", vm_start: "busy" },
        {
          target: {
            running: { action: "wake_target", step: "waking", started_at: "2026-10-13T12:00:00Z" },
            last: null,
          },
        },
      ),
    };
    const { target, vm } = await openDashboard({ status: running });
    expect(await within(target).findByTestId("action-running")).toHaveTextContent(
      "Magic Packet gesendet",
    );
    expect(within(vm).getByText("Wartet, bis der Server bereit ist")).toBeInTheDocument();
    expect(button(target, "Wecken")).toBeDisabled();
    emit({
      ...STATUS,
      actions: actionsSnapshot(
        {},
        {
          target: {
            running: null,
            last: {
              action: "wake_target",
              outcome: "boot_timeout",
              finished_at: "2026-10-13T12:05:00Z",
            },
          },
        },
      ),
    });
    const result = within(target).getByTestId("action-result");
    expect(result).toHaveTextContent("Zuletzt: Server wecken – Der Server war nicht rechtzeitig");
    expect(result).toHaveClass("text-destructive");
  });

  it("explains a missing permission of the API key", async () => {
    const status: StatusSnapshot = {
      ...STATUS_VM_RUNNING,
      vm: { state: "offline", reason: null },
      actions: actionsSnapshot(
        {},
        {
          vm: {
            running: null,
            last: {
              action: "vm_start",
              outcome: "api_forbidden",
              finished_at: "2026-10-13T12:05:00Z",
            },
          },
        },
      ),
    };
    const { vm } = await openDashboard({ status });
    expect(await within(vm).findByTestId("action-result")).toHaveTextContent(
      "Keine Berechtigung: Der API-Key darf VMs nicht starten oder ausschalten. In Unraid die Berechtigung VMS: Update (any) ergänzen.",
    );
  });

  it("explains a rejected request", async () => {
    const { user, target } = await openDashboard({ actionRejection: "busy" });
    await within(target).findByText("Nicht erreichbar");
    await user.click(button(target, "Wecken"));
    expect(await within(target).findByRole("alert")).toHaveTextContent(
      "Nicht ausgeführt: eine andere Aktion läuft gerade.",
    );
  });

  it("lists the enabled action groups in the settings", async () => {
    const { user } = await openDashboard({
      mode: "real",
      info: {
        ...INFO,
        mode: "real",
        actions: { wake: false, vm: true, shutdown: false },
      },
    });
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("action-group-vm")).toHaveTextContent("freigeschaltet");
    expect(screen.getByTestId("action-group-wake")).toHaveTextContent("gesperrt");
    expect(screen.getByTestId("action-group-shutdown")).toHaveTextContent("gesperrt");
  });
});
