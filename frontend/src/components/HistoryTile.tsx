import { useTranslation } from "react-i18next";

import { AUDIT_ACTIONS, AUDIT_RESULTS, type AuditEntry } from "@/api/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const FAILURE_RESULTS = new Set(["invalid_password", "locked", "lockout_started", "rejected"]);

function isKnown<T extends string>(list: readonly T[], value: string): value is T {
  return (list as readonly string[]).includes(value);
}

export function HistoryTile({ entries, className }: { entries: AuditEntry[]; className?: string }) {
  const { t, i18n } = useTranslation();
  const format = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
    dateStyle: "short",
    timeStyle: "medium",
  });

  return (
    <Card className={className}>
      <CardHeader>
        <div>
          <CardTitle>{t("tiles.history.title")}</CardTitle>
          <CardDescription>{t("tiles.history.description")}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {entries.length === 0 ? (
          <p className="text-sm text-muted-foreground italic">{t("tiles.history.empty")}</p>
        ) : (
          <ul className="flex max-h-72 flex-col divide-y overflow-y-auto text-sm">
            {entries.map((entry, index) => (
              <li
                key={`${entry.at}-${String(index)}`}
                className="flex flex-wrap items-baseline justify-between gap-x-3 py-1.5"
              >
                <span>
                  <span className="font-medium">
                    {isKnown(AUDIT_ACTIONS, entry.action)
                      ? t(`audit.actions.${entry.action}`)
                      : t("audit.unknown")}
                  </span>{" "}
                  <span
                    className={cn(
                      FAILURE_RESULTS.has(entry.result)
                        ? "text-destructive"
                        : "text-muted-foreground",
                    )}
                  >
                    {isKnown(AUDIT_RESULTS, entry.result)
                      ? t(`audit.results.${entry.result}`)
                      : t("audit.unknown")}
                  </span>
                </span>
                <span className="text-xs text-muted-foreground">
                  {format.format(new Date(entry.at))} ·{" "}
                  {entry.client_ip === null
                    ? t("audit.local")
                    : t("audit.from", { ip: entry.client_ip })}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
