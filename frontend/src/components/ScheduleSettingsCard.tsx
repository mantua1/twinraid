import { CalendarClock } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ScheduleStatus } from "@/api/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatGap, termWithZone } from "@/lib/schedule";

interface ScheduleSettingsCardProps {
  schedule: ScheduleStatus;
  target: string;
}

/** Zeitplan nur lesend: nächste drei Termine, Wartezeiten, Hinweis zum Takt (Konzept 4.8). */
export function ScheduleSettingsCard({ schedule, target }: ScheduleSettingsCardProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const term = (value: string) =>
    termWithZone(language, value, schedule.timezone, (text, zone) =>
      t("sync.schedule.zone", { term: text, zone }),
    );
  const state = !schedule.enabled
    ? t("settings.schedule.off")
    : schedule.permitted
      ? t("settings.schedule.on")
      : t("settings.schedule.notPermitted");
  const days = schedule.days.map((day) => t(`sync.schedule.days.${day}`)).join(", ");
  return (
    <Card data-testid="settings-schedule">
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <CalendarClock className="size-5" aria-hidden="true" />
            {t("settings.schedule.title")}
          </CardTitle>
          <CardDescription>{t("settings.schedule.description")}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="grid gap-1 text-sm sm:grid-cols-[max-content_1fr] sm:gap-x-6">
          <dt className="font-medium">{t("settings.schedule.state")}</dt>
          <dd data-testid="schedule-state">{state}</dd>
          <dt className="font-medium">{t("settings.schedule.plan")}</dt>
          <dd>
            {t("settings.schedule.planValue", {
              days,
              time: schedule.time,
              zone: schedule.timezone,
            })}
          </dd>
          <dt className="font-medium">{t("settings.schedule.next")}</dt>
          <dd data-testid="schedule-terms">
            {schedule.next_terms.length === 0 ? (
              t("settings.schedule.none")
            ) : (
              <ul>
                {schedule.next_terms.map((value) => (
                  <li key={value}>{term(value)}</li>
                ))}
              </ul>
            )}
          </dd>
          <dt className="font-medium">{t("settings.schedule.wake")}</dt>
          <dd>
            {schedule.wake
              ? t("settings.schedule.wakeOn", { target })
              : t("settings.schedule.wakeOff", { target })}
          </dd>
          <dt className="font-medium">{t("settings.schedule.shutdown")}</dt>
          <dd data-testid="schedule-shutdown">
            {schedule.shutdown
              ? t("settings.schedule.shutdownOn", {
                  minutes: schedule.shutdown_delay_minutes,
                  target,
                })
              : t("settings.schedule.shutdownOff")}
          </dd>
          <dt className="font-medium">{t("settings.schedule.play")}</dt>
          <dd data-testid="schedule-play">
            {schedule.play_shutdown
              ? t("settings.schedule.playOn", { minutes: schedule.play_delay_minutes, target })
              : t("settings.schedule.playOff")}
          </dd>
        </dl>
        {schedule.shortest_gap_minutes !== null && (
          <p className="text-xs text-muted-foreground">
            {t("settings.schedule.gap", { gap: formatGap(schedule.shortest_gap_minutes) })}
          </p>
        )}
        {schedule.conflicts.map((conflict) => (
          <p
            key={conflict.limit}
            className="text-sm text-amber-700 dark:text-amber-400"
            data-testid="schedule-conflict"
          >
            {t("settings.schedule.conflict", {
              limit: conflict.limit,
              minutes: conflict.minutes,
              target,
            })}
          </p>
        ))}
        <p className="text-xs text-muted-foreground">{t("settings.schedule.skip")}</p>
      </CardContent>
    </Card>
  );
}
