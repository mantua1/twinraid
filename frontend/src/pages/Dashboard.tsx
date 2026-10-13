import { useTranslation } from "react-i18next";

import type { ResourceState } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { PlaceholderTile } from "@/components/PlaceholderTile";
import { StatusTile } from "@/components/StatusTile";
import type { DashboardData } from "@/hooks/useDashboardData";

const UNKNOWN: ResourceState = "unknown";

export function Dashboard({ data }: { data: DashboardData }) {
  const { t } = useTranslation();
  const { info, status } = data;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
      <h1 className="text-2xl font-semibold">{t("dashboard.title")}</h1>
      {data.loading && <p className="text-sm text-muted-foreground">{t("dashboard.loading")}</p>}
      {data.infoFailed && <ErrorNotice message={t("errors.connection")} />}
      {!data.infoFailed && data.statusFailed && <ErrorNotice message={t("errors.status")} />}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatusTile
          title={info?.names.source ?? t("tiles.source.fallbackTitle")}
          description={t("tiles.source.description")}
          state={status?.source ?? UNKNOWN}
        />
        <StatusTile
          title={info?.names.target ?? t("tiles.target.fallbackTitle")}
          description={t("tiles.target.description")}
          state={status?.target ?? UNKNOWN}
        />
        <StatusTile
          title={info?.names.vm ?? t("tiles.vm.fallbackTitle")}
          description={t("tiles.vm.description")}
          state={status?.vm ?? UNKNOWN}
        />
        <PlaceholderTile
          title={t("tiles.sync.title")}
          description={t("tiles.sync.description")}
          empty={t("tiles.sync.empty")}
        />
        <PlaceholderTile
          className="sm:col-span-2"
          title={t("tiles.history.title")}
          description={t("tiles.history.description")}
          empty={t("tiles.history.empty")}
        />
      </div>
    </div>
  );
}
