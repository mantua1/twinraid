import { useTranslation } from "react-i18next";

import type { WatchRetry } from "@/api/types";

/** Nächster Versuch der Statusverbindung nach einem Fehler (Konzept 3.5, ab v0.4.4). */
export function RetryLine({ retry }: { retry: WatchRetry | null | undefined }) {
  const { t, i18n } = useTranslation();
  if (retry == null) {
    return null;
  }
  const time = new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "medium" }).format(
    new Date(retry.at),
  );
  return (
    <p
      className="text-xs text-muted-foreground"
      data-testid="watch-retry"
      data-paused={retry.paused}
    >
      {t(retry.paused ? "status.retry.paused" : "status.retry.next", { time })}
    </p>
  );
}
