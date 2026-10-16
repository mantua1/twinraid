import { TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { StatusNotice } from "@/api/types";

interface NoticeListProps {
  notices: readonly StatusNotice[];
  // Name des Servers für die Texte, z. B. „Auf Unraid Groß ist …“.
  target: string;
}

/** Feste Hinweise zu einer Kachel (Konzept 3.5 und 4.1); sie sperren keine Aktion. */
export function NoticeList({ notices, target }: NoticeListProps) {
  const { t } = useTranslation();
  if (notices.length === 0) {
    return null;
  }
  return (
    <ul className="flex flex-col gap-2">
      {notices.map((notice) => (
        <li
          key={notice}
          data-testid="status-notice"
          data-notice={notice}
          className="flex items-start gap-2 rounded-lg border border-amber-600/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-200"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{t(`status.notices.${notice}`, { target })}</span>
        </li>
      ))}
    </ul>
  );
}
