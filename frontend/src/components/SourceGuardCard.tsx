import { ShieldHalf } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { GuardDataset, SourceGuardStatus } from "@/api/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatBytes, formatSince } from "@/lib/format";

interface SourceGuardCardProps {
  guard: SourceGuardStatus | null;
  // Namen von Klein und Groß für die Texte.
  name: string;
  target: string;
}

function DatasetLine({ dataset, language }: { dataset: GuardDataset; language?: string }) {
  const { t } = useTranslation();
  if (dataset.present === null) {
    return t("guard.settings.datasetUnknown");
  }
  if (!dataset.present) {
    return t("guard.settings.datasetMissing");
  }
  const parts = [
    t("guard.settings.datasetValue", {
      size: dataset.referenced !== null ? formatBytes(language, dataset.referenced) : "?",
      compression: dataset.compression ?? "?",
    }),
  ];
  // Eigene Snapshots, Basis und Lesezeichen (ab v0.5.1, Konzept 3.6); ohne Zählung unbekannt.
  if (dataset.snapshots !== null) {
    parts.push(t("guard.settings.datasetSnapshots", { count: dataset.snapshots }));
    parts.push(
      dataset.base_created_at !== null
        ? t("guard.settings.datasetBase", { time: formatSince(language, dataset.base_created_at) })
        : t("guard.settings.datasetNoBase"),
    );
  }
  if (dataset.bookmark === true) {
    parts.push(t("guard.settings.datasetBookmark"));
  }
  return parts.join(" · ");
}

/** Wächter auf Klein in den Einstellungen, nur lesend (Konzept 3.6, ab v0.5.0). */
export function SourceGuardCard({ guard, name, target }: SourceGuardCardProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const details = guard?.details ?? null;
  return (
    <Card data-testid="settings-source-guard">
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <ShieldHalf className="size-5" aria-hidden="true" />
            {t("guard.settings.title", { name })}
          </CardTitle>
          <CardDescription>{t("guard.settings.description", { name })}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {details === null ? (
          <p className="text-sm text-muted-foreground">
            {guard?.state === "not_mounted" ? t("guard.notMounted") : t("guard.settings.noDetails")}
          </p>
        ) : (
          <dl className="grid gap-3 text-sm sm:grid-cols-[max-content_1fr] sm:gap-x-6">
            <dt className="font-medium">{t("guard.settings.version")}</dt>
            <dd data-testid="guard-version">{details.version}</dd>
            {/* Das genaue Alter des Herzschlags steht nur hier, nicht in der Kachel (D1). */}
            <dt className="font-medium">{t("guard.settings.heartbeat")}</dt>
            <dd data-testid="guard-heartbeat">
              {t("guard.settings.heartbeatValue", { count: guard?.heartbeat_age_seconds ?? 0 })}
            </dd>
            <dt className="font-medium">{t("guard.settings.startedAt")}</dt>
            <dd>{formatSince(language, details.started_at)}</dd>
            <dt className="font-medium">{t("guard.settings.limits")}</dt>
            <dd>
              {t("guard.settings.limitsValue", {
                max: details.limits.max_stop_minutes,
                heartbeat: details.limits.heartbeat_timeout_seconds,
                stop: details.limits.stop_timeout_seconds,
                pause: details.limits.min_run_interval_minutes,
              })}
            </dd>
            <dt className="font-medium">{t("guard.settings.maxDeletions")}</dt>
            <dd>{new Intl.NumberFormat(language).format(details.limits.max_deletions)}</dd>
            <dt className="font-medium">{t("guard.settings.snapshots", { name })}</dt>
            <dd>
              {t("guard.settings.snapshotsValue", {
                days: details.limits.snapshot_keep_days,
                reserve: details.limits.snapshot_reserve_percent,
              })}
            </dd>
            <dt className="font-medium">{t("guard.settings.target")}</dt>
            <dd>
              {details.target_configured
                ? t("guard.settings.targetConfigured", { port: details.target_port })
                : t("guard.settings.targetMissing")}
            </dd>
            <dt className="font-medium">{t("guard.settings.key")}</dt>
            <dd data-testid="guard-key">
              {details.target_key ? t("guard.settings.keyPresent") : t("guard.settings.keyMissing")}
            </dd>
            <dt className="font-medium">{t("guard.settings.knownHost", { target })}</dt>
            <dd data-testid="guard-known-host">
              {details.target_known_host
                ? t("guard.settings.knownHostPresent")
                : t("guard.settings.knownHostMissing")}
            </dd>
            <dt className="font-medium">{t("guard.settings.allowed")}</dt>
            <dd>
              {details.allowed.length === 0 ? (
                t("guard.settings.none")
              ) : (
                <ul data-testid="guard-allowed">
                  {details.allowed.map((item) => (
                    <li key={item.name}>
                      <span className="font-mono">{item.name}</span>:{" "}
                      {t(`guard.containerStates.${item.state}`)}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt className="font-medium">{t("guard.settings.datasets")}</dt>
            <dd>
              {details.zfs.datasets.length === 0 ? (
                t("guard.settings.none")
              ) : (
                <ul data-testid="guard-datasets">
                  {details.zfs.datasets.map((item) => (
                    <li key={item.name}>
                      <span className="font-mono">mainpool/{item.name}</span>:{" "}
                      <DatasetLine dataset={item} language={language} />
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt className="font-medium">{t("guard.settings.rejected")}</dt>
            <dd data-testid="guard-rejected">{details.rejected_count}</dd>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
