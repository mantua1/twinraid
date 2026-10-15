import { LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ResourceActivity } from "@/api/types";

/** Laufende Aktion mit aktuellem Schritt, sonst das letzte Ergebnis (Konzept 4.2). */
export function ActivityLine({ activity }: { activity: ResourceActivity | undefined }) {
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
  const time = new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "short" }).format(
    new Date(last.finished_at),
  );
  const text = t("actions.last", {
    action: t(`actions.names.${last.action}`),
    outcome: t(`actions.outcomes.${last.outcome}`),
    time,
  });
  return (
    <p
      data-testid="action-result"
      data-outcome={last.outcome}
      className={
        last.outcome === "success" ? "text-xs text-muted-foreground" : "text-sm text-destructive"
      }
    >
      {text}
    </p>
  );
}
