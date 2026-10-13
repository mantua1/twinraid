import { FlaskConical } from "lucide-react";
import { useTranslation } from "react-i18next";

/** Dauerhaftes, nicht schließbares Banner im Simulationsmodus (Konzept 6.4). */
export function MockBanner() {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      data-testid="mock-banner"
      className="sticky top-0 z-50 border-b-2 border-amber-600 bg-amber-300 text-amber-950 dark:border-amber-400 dark:bg-amber-500"
    >
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-2">
        <FlaskConical className="size-5 shrink-0" aria-hidden="true" />
        <p className="text-sm">
          <strong className="mr-2 font-bold tracking-wide uppercase">
            {t("mockBanner.title")}
          </strong>
          <span>{t("mockBanner.description")}</span>
        </p>
      </div>
    </div>
  );
}
