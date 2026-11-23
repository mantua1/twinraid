import { useTranslation } from "react-i18next";

import type { AutoShutdownStatus } from "@/api/types";
import { formatTime } from "@/lib/format";

interface AutoShutdownLineProps {
  auto: AutoShutdownStatus | null | undefined;
  target: string;
}

/** Vorgemerktes Herunterfahren in der Kachel Groß (Konzept 4.1 und 4.2, ab v0.6.0). */
export function AutoShutdownLine({ auto, target }: AutoShutdownLineProps) {
  const { t, i18n } = useTranslation();
  if (auto == null) {
    return null;
  }
  const kind = t(
    `tiles.target.autoShutdown.kinds.${auto.kinds.length > 1 ? "both" : (auto.kinds[0] ?? "after_sync")}`,
  );
  const text =
    auto.at !== null
      ? t("tiles.target.autoShutdown.at", {
          time: formatTime(i18n.resolvedLanguage, auto.at),
          kind,
        })
      : t("tiles.target.autoShutdown.waiting", {
          kind,
          reason: t(`tiles.target.autoShutdown.reasons.${auto.reason ?? "busy"}`),
        });
  return (
    <div className="flex flex-col gap-1" data-testid="auto-shutdown">
      <p className="text-sm">{text}</p>
      <p className="text-xs text-muted-foreground">
        {t("tiles.target.autoShutdown.keepHint", { target })}
      </p>
    </div>
  );
}
