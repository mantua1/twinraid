import { useTranslation } from "react-i18next";

import type { ScheduleStatus } from "@/api/types";
import type { BlockerValues } from "@/components/ActionPanel";
import { formatTime } from "@/lib/format";
import { termWithZone } from "@/lib/schedule";
import { useReasonText } from "@/lib/syncTexts";
import { cn } from "@/lib/utils";

interface ScheduleLinesProps {
  schedule: ScheduleStatus;
  blockerValues: BlockerValues;
}

/** Zeitplan in der Kachel Sync (Konzept 4.1, „Kachel Sync ab v0.6.0“). */
export function ScheduleLines({ schedule, blockerValues }: ScheduleLinesProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const reasonText = useReasonText();
  const term = (value: string) =>
    termWithZone(language, value, schedule.timezone, (text, zone) =>
      t("sync.schedule.zone", { term: text, zone }),
    );
  const target = blockerValues.target;
  const last = schedule.last;
  const lastReason = last === null ? null : reasonText(last.outcome, last.reason, blockerValues);
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="sync-schedule">
      {!schedule.enabled ? (
        <p className="text-xs text-muted-foreground">{t("sync.schedule.off")}</p>
      ) : !schedule.permitted ? (
        <p className="text-xs text-muted-foreground">{t("sync.schedule.notPermitted")}</p>
      ) : (
        <>
          {schedule.next_terms.length > 0 && (
            <p data-testid="schedule-next">
              {t("sync.schedule.next", { term: term(schedule.next_terms[0] ?? "") })}
            </p>
          )}
          <p className="text-xs text-muted-foreground" data-testid="schedule-wake">
            {schedule.wake
              ? schedule.shutdown
                ? t("sync.schedule.wakeAndShutdown", { target })
                : t("sync.schedule.wakeOnly", { target })
              : t("sync.schedule.noWake", { target })}
          </p>
        </>
      )}
      {schedule.waiting !== null && (
        <p className="text-amber-700 dark:text-amber-400" data-testid="schedule-waiting">
          {t("sync.schedule.waiting", {
            reason: t(`actions.blockers.${schedule.waiting.reason}`, { ...blockerValues }),
            next: formatTime(language, schedule.waiting.next_attempt_at),
            until: formatTime(language, schedule.waiting.until),
          })}
        </p>
      )}
      {last !== null && (
        <div data-testid="schedule-last" data-outcome={last.outcome}>
          <p className={cn(last.outcome !== "success" && "text-destructive")}>
            {t("sync.schedule.last", {
              term: term(last.scheduled_for),
              outcome: t(`actions.outcomes.${last.outcome}`),
            })}
          </p>
          {lastReason !== null && (
            <p className="text-xs text-destructive">
              {t("sync.schedule.lastReason", { reason: lastReason })}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
