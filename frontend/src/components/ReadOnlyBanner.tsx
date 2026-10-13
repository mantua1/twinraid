import { Eye } from "lucide-react";
import { useTranslation } from "react-i18next";

/** Hinweis im Modus real, solange Aktionen gesperrt sind (Konzept 6.4). */
export function ReadOnlyBanner() {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      data-testid="read-only-banner"
      className="border-b border-sky-600/40 bg-sky-500/10 text-sky-900 dark:text-sky-200"
    >
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-2">
        <Eye className="size-5 shrink-0" aria-hidden="true" />
        <p className="text-sm">
          <strong className="mr-2 font-semibold">{t("readOnlyBanner.title")}</strong>
          <span>{t("readOnlyBanner.description")}</span>
        </p>
      </div>
    </div>
  );
}
