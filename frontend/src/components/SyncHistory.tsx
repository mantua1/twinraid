import { useTranslation } from "react-i18next";

import type { SyncHistoryEntry } from "@/api/types";
import { formatSince } from "@/lib/format";
import { QUIET_OUTCOMES, uncountedKind, useReasonText, useRunDetails } from "@/lib/syncTexts";
import { cn } from "@/lib/utils";

interface SyncHistoryProps {
  entries: SyncHistoryEntry[];
  names: { source: string; target: string };
}

/** Die letzten 20 Läufe aus der Sync-Historie, neueste zuerst (Konzept 4.1 und 5.3). */
export function SyncHistory({ entries, names }: SyncHistoryProps) {
  const { t, i18n } = useTranslation();
  const details = useRunDetails();
  const reasonText = useReasonText();
  if (entries.length === 0) {
    return null;
  }
  return (
    <details className="text-sm" data-testid="sync-history">
      <summary className="cursor-pointer font-medium">
        {t("sync.history.title", { count: entries.length })}
      </summary>
      <ol className="mt-2 flex flex-col gap-2">
        {entries.map((entry) => {
          const reason = reasonText(entry.result, entry.reason, names);
          return (
            <li key={entry.id} className="border-t pt-2" data-testid="sync-history-entry">
              <p>
                <span className="tabular-nums">
                  {formatSince(i18n.resolvedLanguage, entry.finished_at)}
                </span>
                {" · "}
                {t(`sync.kinds.${entry.kind}`)}
                {" · "}
                <span className="text-muted-foreground">{details(entry, names.target)}</span>
              </p>
              <p
                className={cn(
                  "text-xs",
                  QUIET_OUTCOMES.has(entry.result) ? "text-muted-foreground" : "text-destructive",
                )}
              >
                {t(`actions.outcomes.${entry.result}`)}
                {reason !== null && ` ${t("sync.reason", { reason })}`}
              </p>
              {entry.shares.map((share) => {
                const kind = uncountedKind(share);
                return kind === null ? null : (
                  <p
                    key={share.name}
                    className="text-xs text-muted-foreground"
                    data-testid="sync-history-uncounted"
                  >
                    {share.name}: {t(`sync.uncounted.${kind}`)}
                  </p>
                );
              })}
            </li>
          );
        })}
      </ol>
    </details>
  );
}
