import { useTranslation } from "react-i18next";

import type { ResourceInfo, StatusNotice } from "@/api/types";
import { ActionPanel, type PanelAction } from "@/components/ActionPanel";
import { ActivityLine } from "@/components/ActivityLine";
import { CaChangeNotice } from "@/components/CaChangeNotice";
import { ErrorNotice } from "@/components/ErrorNotice";
import { HistoryTile } from "@/components/HistoryTile";
import { NoticeList } from "@/components/NoticeList";
import { RetryLine } from "@/components/RetryLine";
import { SourceGuardPanel } from "@/components/SourceGuardPanel";
import { StatusTile } from "@/components/StatusTile";
import { SyncTile } from "@/components/SyncTile";
import type { DashboardData } from "@/hooks/useDashboardData";
import { formatTime, roundUpToMinute } from "@/lib/format";

const UNKNOWN: ResourceInfo = { state: "unknown", reason: null };
const tileOf = (notice: StatusNotice) =>
  notice.startsWith("source_") ? "source" : notice.startsWith("vm_") ? "vm" : "target";

export function Dashboard({ data }: { data: DashboardData }) {
  const { t, i18n } = useTranslation();
  const { info, tls, stream } = data;
  const status = stream.status;
  const actions = status?.actions ?? null;
  const names = {
    source: info?.names.source ?? t("tiles.source.fallbackTitle"),
    target: info?.names.target ?? t("tiles.target.fallbackTitle"),
    vm: info?.names.vm ?? t("tiles.vm.fallbackTitle"),
  };
  const vmRunning = status?.vm.state === "online" || status?.vm.state === "stopping";
  const notices = status?.notices ?? [];
  const sourceNotices = notices.filter((notice) => tileOf(notice) === "source");
  const targetNotices = notices.filter((notice) => tileOf(notice) === "target");
  const vmNotices = notices.filter((notice) => tileOf(notice) === "vm");
  // Pause des Wächters zwischen zwei Läufen (Konzept 3.6), auf die Minute aufgerundet. Der
  // Wächter setzt run_allowed_at nur, solange die Pause gilt; ein veralteter Status fehlt hier.
  const runAllowedAt = status?.source_guard.details?.run_allowed_at ?? null;
  const nextSync =
    runAllowedAt !== null ? formatTime(i18n.resolvedLanguage, roundUpToMinute(runAllowedAt)) : null;
  // Für die Begründung „Auf <Server> laufen noch weitere VMs: <Namen> …“ (Konzept 4.2).
  const otherVms = status?.other_vms_running ?? [];
  const blockerValues = {
    source: names.source,
    target: names.target,
    vms: otherVms.join(", "),
  };
  const shortTime = (value: string) =>
    new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "short" }).format(new Date(value));

  const targetActions: PanelAction[] = [
    { name: "wake_target", label: t("actions.buttons.wake_target"), variant: "default" },
    {
      name: "shutdown_target",
      label: t("actions.buttons.shutdown_target"),
      confirm: vmRunning
        ? {
            title: t("actions.dialog.shutdownWithVm.title"),
            description: t("actions.dialog.shutdownWithVm.description", names),
            confirmLabel: t("actions.dialog.shutdownWithVm.confirm"),
          }
        : {
            title: t("actions.dialog.shutdown.title", names),
            description: t("actions.dialog.shutdown.description"),
            confirmLabel: t("actions.dialog.shutdown.confirm"),
          },
    },
  ];
  const vmActions: PanelAction[] = [
    { name: "vm_start", label: t("actions.buttons.vm_start"), variant: "default" },
    {
      name: "vm_stop",
      label: t("actions.buttons.vm_stop"),
      confirm: {
        title: t("actions.dialog.vmStop.title", names),
        description: t("actions.dialog.vmStop.description", {
          count: info?.vm_shutdown_timeout_minutes ?? 0,
        }),
        confirmLabel: t("actions.dialog.vmStop.confirm"),
      },
    },
  ];
  // Nur kurz nach einem an der VM gescheiterten Herunterfahren von Groß (Konzept 4.2, E10).
  if (actions?.forced_shutdown_until != null) {
    vmActions.push({
      name: "shutdown_target_force_vm",
      label: t("actions.buttons.shutdown_target_force_vm"),
      variant: "destructive",
      note: t("actions.validUntil", { time: shortTime(actions.forced_shutdown_until) }),
      confirm: {
        title: t("actions.dialog.forceAndShutdown.title", names),
        description: t("actions.dialog.forceAndShutdown.description", names),
        confirmLabel: t("actions.dialog.forceAndShutdown.confirm"),
        destructive: true,
      },
    });
  }
  // „Erzwingen“ erscheint erst, wenn ein Herunterfahren läuft oder gescheitert ist (Konzept 4.1).
  if (actions?.force_available_at != null) {
    vmActions.push({
      name: "vm_force_stop",
      label: t("actions.buttons.vm_force_stop"),
      variant: "destructive",
      confirm: {
        title: t("actions.dialog.force.title", names),
        description: t("actions.dialog.force.description"),
        confirmLabel: t("actions.dialog.force.confirm"),
        destructive: true,
      },
    });
  }
  const checkedAt =
    status?.checked_at != null
      ? new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "medium" }).format(
          new Date(status.checked_at),
        )
      : null;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">{t("dashboard.title")}</h1>
        {checkedAt !== null && (
          <p className="text-xs text-muted-foreground" data-testid="checked-at">
            {t("dashboard.checkedAt", { time: checkedAt })}
          </p>
        )}
      </div>
      {tls?.ca_change != null && (
        <CaChangeNotice change={tls.ca_change} onAcknowledge={data.acknowledgeCaChange} />
      )}
      {data.loading && <p className="text-sm text-muted-foreground">{t("dashboard.loading")}</p>}
      {data.infoFailed && <ErrorNotice message={t("errors.connection")} />}
      {!data.infoFailed && stream.state === "workerUnavailable" && (
        <ErrorNotice message={t("errors.status")} />
      )}
      {!data.infoFailed && stream.state === "reconnecting" && (
        <ErrorNotice message={t("errors.liveConnection")} />
      )}
      {tls?.renewal_failed === true && <ErrorNotice message={t("certificate.renewalFailed")} />}
      {tls?.server.expires_soon === true && (
        <ErrorNotice message={t("certificate.serverExpires", { count: tls.server.days_left })} />
      )}
      {tls?.ca?.expires_soon === true && (
        <ErrorNotice message={t("certificate.caExpires", { count: tls.ca.days_left })} />
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatusTile
          testId="tile-source"
          title={names.source}
          description={t("tiles.source.description")}
          info={status?.source ?? UNKNOWN}
        >
          {status != null && (
            <SourceGuardPanel
              guard={status.source_guard}
              notices={sourceNotices}
              source={names.source}
            />
          )}
        </StatusTile>
        <StatusTile
          testId="tile-target"
          title={names.target}
          description={t("tiles.target.description")}
          info={status?.target ?? UNKNOWN}
        >
          <NoticeList notices={targetNotices} target={names.target} />
          {/* Nur Anzeige: ausgeschaltete VMs meldet guard.sh nicht (Konzept 4.1, ab v0.4.4). */}
          {otherVms.length > 0 && (
            <p className="text-sm" data-testid="other-vms">
              {t("tiles.target.otherVms", { vms: blockerValues.vms })}
            </p>
          )}
          <ActivityLine
            activity={actions?.target}
            status={status}
            tile="target"
            vmName={names.vm}
          />
          <ActionPanel
            actions={targetActions}
            snapshot={actions}
            onRun={data.runAction}
            blockerValues={blockerValues}
          />
        </StatusTile>
        <StatusTile
          testId="tile-vm"
          title={names.vm}
          description={t("tiles.vm.description")}
          info={status?.vm ?? UNKNOWN}
        >
          <RetryLine retry={status?.watch_retry} />
          <NoticeList notices={vmNotices} target={names.target} />
          <ActivityLine activity={actions?.vm} status={status} tile="vm" vmName={names.vm} />
          <ActionPanel
            actions={vmActions}
            snapshot={actions}
            onRun={data.runAction}
            blockerValues={blockerValues}
          />
        </StatusTile>
        <SyncTile
          className="sm:col-span-2"
          status={status}
          names={names}
          nextSync={nextSync}
          blockerValues={blockerValues}
          onRun={data.runAction}
          onCancel={data.cancelSync}
        />
        <HistoryTile className="sm:col-span-2 lg:col-span-1" entries={data.audit} />
      </div>
    </div>
  );
}
