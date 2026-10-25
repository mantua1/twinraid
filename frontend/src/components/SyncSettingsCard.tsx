import { Layers } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { GuardCheck, SyncLevels } from "@/api/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatBytes, formatSince } from "@/lib/format";
import { cn } from "@/lib/utils";

interface SyncSettingsCardProps {
  levels: SyncLevels;
  // Letztes guard.sh sync check aus dem Status des Wächters; null: noch keins.
  check: GuardCheck | null;
  names: { source: string; target: string };
}

/** Ja, nein oder unbekannt für eine Ebene. */
function Mark({ value }: { value: boolean | null }) {
  const { t } = useTranslation();
  if (value === null) {
    return <span className="text-muted-foreground">{t("settings.sync.unknown")}</span>;
  }
  return (
    <span className={cn(!value && "text-destructive")}>
      {value ? t("settings.sync.yes") : t("settings.sync.no")}
    </span>
  );
}

/** Die drei Ebenen und die Spiegel auf Groß aus dem letzten check (Konzept 4.3, 3.7). */
export function SyncSettingsCard({ levels, check, names }: SyncSettingsCardProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  // Alle Namen, die auf irgendeiner Ebene stehen, zuerst die aus twinraid.yaml.
  const shares = [...levels.configured];
  for (const name of [...(levels.source ?? []), ...(levels.target ?? [])].sort()) {
    if (!shares.includes(name)) {
      shares.push(name);
    }
  }
  const has = (list: string[] | null, name: string) => (list === null ? null : list.includes(name));
  const summary = check?.summary ?? null;
  const when = (value: string | null) => (value === null ? "–" : formatSince(language, value));
  const header = "pb-1 pr-4 text-left font-medium";
  return (
    <Card data-testid="settings-sync">
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <Layers className="size-5" aria-hidden="true" />
            {t("settings.sync.title")}
          </CardTitle>
          <CardDescription>{t("settings.sync.description", names)}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="sync-levels">
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th className={header}>{t("settings.sync.share")}</th>
                <th className={header}>twinraid.yaml</th>
                <th className={header}>{names.source} (source-guard.conf)</th>
                <th className="pb-1 text-left font-medium">{names.target} (mirror.conf)</th>
              </tr>
            </thead>
            <tbody>
              {shares.map((name) => (
                <tr key={name} className="border-t">
                  <td className="py-1.5 pr-4 font-mono">{name}</td>
                  <td className="py-1.5 pr-4">
                    <Mark value={levels.configured.includes(name)} />
                  </td>
                  <td className="py-1.5 pr-4">
                    <Mark value={has(levels.source, name)} />
                  </td>
                  <td className="py-1.5">
                    <Mark value={has(levels.target, name)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {check === null ? (
          <p className="text-sm text-muted-foreground" data-testid="sync-no-check">
            {t("settings.sync.noCheck", names)}
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            <p
              className={cn("text-sm", check.result !== "ok" && "text-destructive")}
              data-testid="sync-check"
            >
              {t("settings.sync.checkAt", {
                target: names.target,
                time: formatSince(language, check.at),
                result: t(`sync.check.${check.result}`, names),
              })}
            </p>
            {summary !== null && (
              <p className="text-sm text-muted-foreground" data-testid="sync-check-summary">
                {t("settings.sync.summary", {
                  free: summary.available === null ? "?" : formatBytes(language, summary.available),
                  size: summary.size === null ? "?" : formatBytes(language, summary.size),
                  reserve: summary.reserve_percent ?? "?",
                  keep: summary.keep_days ?? "?",
                  deleg: summary.deleg,
                  receive: summary.receive,
                  clones: summary.clones ?? "?",
                })}
              </p>
            )}
            {check.mirrors.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="sync-mirrors">
                  <thead className="text-xs text-muted-foreground">
                    <tr>
                      <th className={header}>{t("settings.sync.mirror")}</th>
                      <th className={header}>{t("settings.sync.state")}</th>
                      <th className={cn(header, "text-right")}>{t("settings.sync.snapshots")}</th>
                      <th className={header}>{t("settings.sync.newest")}</th>
                      <th className={header}>{t("settings.sync.oldest")}</th>
                      <th className="pb-1 text-left font-medium">{t("settings.sync.props")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {check.mirrors.map((mirror) => (
                      <tr key={mirror.name} className="border-t">
                        <td className="py-1.5 pr-4 font-mono">{mirror.name}</td>
                        <td
                          className={cn(
                            "py-1.5 pr-4",
                            mirror.state === "foreign" && "text-destructive",
                          )}
                        >
                          {t(`sync.mirrorStates.${mirror.state}`)}
                        </td>
                        <td className="py-1.5 pr-4 text-right tabular-nums">
                          {mirror.count ?? "–"}
                        </td>
                        <td className="py-1.5 pr-4 whitespace-nowrap">
                          {when(mirror.newest_received)}
                        </td>
                        <td className="py-1.5 pr-4 whitespace-nowrap">
                          {when(mirror.oldest_created)}
                        </td>
                        <td
                          className={cn(
                            "py-1.5",
                            mirror.props === "mismatch" && "text-destructive",
                          )}
                        >
                          {mirror.props ?? "–"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
