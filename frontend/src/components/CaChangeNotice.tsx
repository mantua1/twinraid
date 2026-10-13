import { ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { CaChangeInfo } from "@/api/types";
import { Button } from "@/components/ui/button";

interface CaChangeNoticeProps {
  change: CaChangeInfo;
  onAcknowledge: () => Promise<void>;
}

/** Bleibt sichtbar, bis der Nutzer bestätigt, die neue CA installiert zu haben (Konzept 5.2). */
export function CaChangeNotice({ change, onAcknowledge }: CaChangeNoticeProps) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const date = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
    dateStyle: "long",
    timeStyle: "short",
  }).format(new Date(change.changed_at));

  return (
    <div
      role="alert"
      data-testid="ca-change-notice"
      className="flex flex-col gap-3 rounded-lg border-2 border-destructive bg-destructive/10 px-4 py-4 text-sm"
    >
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-destructive" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-semibold text-destructive">{t("caChange.title")}</p>
          <p>{t("caChange.when", { date, reason: t(`caChange.reasons.${change.reason}`) })}</p>
          <p>{t("caChange.action")}</p>
          {failed && <p className="text-destructive">{t("caChange.failed")}</p>}
        </div>
      </div>
      <Button
        className="self-start"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setFailed(false);
          onAcknowledge()
            .catch(() => {
              setFailed(true);
            })
            .finally(() => {
              setBusy(false);
            });
        }}
      >
        {t("caChange.acknowledge")}
      </Button>
    </div>
  );
}
