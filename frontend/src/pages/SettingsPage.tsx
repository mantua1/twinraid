import { ArrowLeft, Download, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CERTIFICATE_URL } from "@/api/client";
import type { TlsInfo } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface SettingsPageProps {
  tls: TlsInfo | null;
  onBack: () => void;
}

export function SettingsPage({ tls, onBack }: SettingsPageProps) {
  const { t, i18n } = useTranslation();
  const validUntil =
    tls === null
      ? ""
      : new Intl.DateTimeFormat(i18n.resolvedLanguage, { dateStyle: "long" }).format(
          new Date(tls.not_after),
        );

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
      <div className="flex items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">{t("settings.title")}</h1>
        <Button variant="outline" size="sm" onClick={onBack}>
          <ArrowLeft aria-hidden="true" />
          {t("settings.back")}
        </Button>
      </div>
      <Card>
        <CardHeader>
          <div>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-5" aria-hidden="true" />
              {t("settings.certificate.title")}
            </CardTitle>
            <CardDescription>{t("settings.certificate.description")}</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {tls === null ? (
            <ErrorNotice message={t("settings.certificate.unavailable")} />
          ) : (
            <dl className="grid gap-3 text-sm sm:grid-cols-[max-content_1fr] sm:gap-x-6">
              <dt className="font-medium">{t("settings.certificate.source")}</dt>
              <dd>{t(`settings.certificate.sources.${tls.source}`)}</dd>
              <dt className="font-medium">{t("settings.certificate.validUntil")}</dt>
              <dd>
                {validUntil} ({t("settings.certificate.daysLeft", { count: tls.days_left })})
              </dd>
              <dt className="font-medium">{t("settings.certificate.fingerprint")}</dt>
              <dd>
                <code
                  data-testid="fingerprint"
                  className="block rounded bg-muted px-2 py-1 font-mono text-xs break-all"
                >
                  {tls.fingerprint_sha256}
                </code>
                <p className="mt-1 text-muted-foreground">{t("settings.certificate.compare")}</p>
              </dd>
            </dl>
          )}
          <a
            href={CERTIFICATE_URL}
            download="twinraid.crt"
            className={buttonVariants({ variant: "default", className: "self-start" })}
          >
            <Download aria-hidden="true" />
            {t("settings.certificate.download")}
          </a>
        </CardContent>
      </Card>
    </div>
  );
}
