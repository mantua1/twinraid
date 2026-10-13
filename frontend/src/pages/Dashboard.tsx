import { useTranslation } from "react-i18next";

import type { ResourceInfo } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { HistoryTile } from "@/components/HistoryTile";
import { PlaceholderTile } from "@/components/PlaceholderTile";
import { StatusTile } from "@/components/StatusTile";
import type { DashboardData } from "@/hooks/useDashboardData";

const UNKNOWN: ResourceInfo = { state: "unknown", reason: null };

export function Dashboard({ data }: { data: DashboardData }) {
  const { t, i18n } = useTranslation();
  const { info, tls, stream } = data;
  const status = stream.status;
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
          title={info?.names.source ?? t("tiles.source.fallbackTitle")}
          description={t("tiles.source.description")}
          info={status?.source ?? UNKNOWN}
        />
        <StatusTile
          title={info?.names.target ?? t("tiles.target.fallbackTitle")}
          description={t("tiles.target.description")}
          info={status?.target ?? UNKNOWN}
        />
        <StatusTile
          title={info?.names.vm ?? t("tiles.vm.fallbackTitle")}
          description={t("tiles.vm.description")}
          info={status?.vm ?? UNKNOWN}
        />
        <PlaceholderTile
          title={t("tiles.sync.title")}
          description={t("tiles.sync.description")}
          empty={status?.job_running === true ? t("tiles.sync.running") : t("tiles.sync.empty")}
        />
        <HistoryTile className="sm:col-span-2" entries={data.audit} />
      </div>
    </div>
  );
}
