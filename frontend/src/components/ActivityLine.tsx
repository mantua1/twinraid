import { LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ActionResult, ResourceActivity, StatusSnapshot } from "@/api/types";
import { cn } from "@/lib/utils";

interface ActivityLineProps {
  activity: ResourceActivity | undefined;
  status: StatusSnapshot | null;
  // Kachel, in der die Zeile steht; Hinweise auf die VM-Kachel nennen sie in der von Groß.
  tile: "target" | "vm";
  vmName: string;
}

type Hint =
  | { key: "forceNow" }
  | { key: "forceAndShutdownUntil"; until: string }
  | { key: "deviceBusyVms"; vms: string };

// Ergebnisse, deren Hinweis vom aktuellen Status abhängt (Konzept 4.2, ab v0.4.1).
const CONDITIONAL = new Set([
  "vm_stop_timeout",
  "boot_timeout",
  "shutdown_timeout",
  "other_vms_running",
  "vm_device_busy",
]);

/**
 * Der Hinweis, was jetzt zu tun ist, solange er nach dem aktuellen Status zutrifft.
 * undefined: das Ergebnis hat keinen bedingten Hinweis; null: er trifft nicht mehr zu.
 */
function currentHint(
  last: ActionResult,
  status: StatusSnapshot | null,
): Hint | "checkWake" | "checkShutdown" | "stillApplies" | null | undefined {
  if (!CONDITIONAL.has(last.outcome)) {
    return undefined;
  }
  if (status === null) {
    return null;
  }
  switch (last.outcome) {
    case "vm_stop_timeout":
      if (status.actions.forced_shutdown_until !== null) {
        return { key: "forceAndShutdownUntil", until: status.actions.forced_shutdown_until };
      }
      return status.actions.availability.vm_force_stop === null ? { key: "forceNow" } : null;
    case "boot_timeout":
      return status.target.state === "offline" ? "checkWake" : null;
    case "other_vms_running":
      // Die Begründung mit den Namen steht schon am gesperrten Button.
      return status.other_vms_running?.length ? "stillApplies" : null;
    case "vm_device_busy":
      // Solange weitere VMs laufen, mit ihren Namen (Konzept 3.5, ab v0.5.1).
      return status.other_vms_running?.length
        ? { key: "deviceBusyVms", vms: status.other_vms_running.join(", ") }
        : null;
    default:
      return status.target.state === "online" ? "checkShutdown" : null;
  }
}

/** Laufende Aktion mit aktuellem Schritt, sonst das letzte Ergebnis (Konzept 4.2). */
export function ActivityLine({ activity, status, tile, vmName }: ActivityLineProps) {
  const { t, i18n } = useTranslation();
  if (activity === undefined) {
    return null;
  }
  const { running, last } = activity;
  if (running !== null) {
    return (
      <p className="flex items-center gap-2 text-sm" data-testid="action-running">
        <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />
        {t(`actions.steps.${running.step}`)}
      </p>
    );
  }
  if (last === null) {
    return null;
  }
  const time = (value: string) =>
    new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "short" }).format(new Date(value));
  const text = t("actions.last", {
    action: t(`actions.names.${last.action}`),
    outcome: t(`actions.outcomes.${last.outcome}`),
    time: time(last.finished_at),
  });
  const hint = currentHint(last, status);
  let hintText: string | null = null;
  if (hint === "checkWake" || hint === "checkShutdown") {
    hintText = t(`actions.hints.${hint}`);
  } else if (hint && hint !== "stillApplies" && hint.key === "deviceBusyVms") {
    hintText = t("actions.hints.deviceBusyVms", { vms: hint.vms });
  } else if (hint && hint !== "stillApplies") {
    const own =
      hint.key === "forceNow"
        ? t("actions.hints.forceNow")
        : t("actions.hints.forceAndShutdownUntil", { time: time(hint.until) });
    hintText = tile === "vm" ? own : t("actions.hints.inVmTile", { vm: vmName, hint: own });
  }
  // Ein Fehler, dessen Hinweis nicht mehr zutrifft, ist nur noch Verlauf.
  const current = last.outcome !== "success" && hint !== null;
  return (
    <div data-testid="action-result" data-outcome={last.outcome} className="flex flex-col gap-1">
      <p className={cn(current ? "text-sm text-destructive" : "text-xs text-muted-foreground")}>
        {text}
      </p>
      {hintText !== null && (
        <p className="text-sm text-destructive" data-testid="action-hint-current">
          {hintText}
        </p>
      )}
    </div>
  );
}
