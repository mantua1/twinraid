import { ArrowLeft, Download, Lock, LockOpen, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CA_CERTIFICATE_URL } from "@/api/client";
import type { AppInfo, TlsInfo } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface SettingsPageProps {
  info: AppInfo | null;
  tls: TlsInfo | null;
  onBack: () => void;
}

function Fingerprint({ value, testId }: { value: string; testId: string }) {
  return (
    <code
      data-testid={testId}
      className="block rounded bg-muted px-2 py-1 font-mono text-xs break-all"
    >
      {value}
    </code>
  );
}

function CertificateCard({ tls }: { tls: TlsInfo | null }) {
  const { t, i18n } = useTranslation();
  const date = (value: string) =>
    new Intl.DateTimeFormat(i18n.resolvedLanguage, { dateStyle: "long" }).format(new Date(value));

  if (tls === null) {
    return <ErrorNotice message={t("settings.certificate.unavailable")} />;
  }
  const { ca, server } = tls;
  return (
    <div className="flex flex-col gap-5">
      {tls.renewal_failed && <ErrorNotice message={t("certificate.renewalFailed")} />}
      {tls.source === "ca" && ca !== null ? (
        <section className="flex flex-col gap-3" aria-labelledby="ca-heading">
          <h2 id="ca-heading" className="font-semibold">
            {t("settings.certificate.ca.title")}
          </h2>
          <p className="text-sm text-muted-foreground">{t("settings.certificate.ca.install")}</p>
          <dl className="grid gap-3 text-sm sm:grid-cols-[max-content_1fr] sm:gap-x-6">
            <dt className="font-medium">{t("settings.certificate.ca.name")}</dt>
            <dd>{ca.name}</dd>
            <dt className="font-medium">{t("settings.certificate.validUntil")}</dt>
            <dd>
              {date(ca.not_after)} ({t("settings.certificate.daysLeft", { count: ca.days_left })})
            </dd>
            <dt className="font-medium">{t("settings.certificate.fingerprint")}</dt>
            <dd>
              <Fingerprint value={ca.fingerprint_sha256} testId="ca-fingerprint" />
            </dd>
            <dt className="font-medium">{t("settings.certificate.ca.sha1")}</dt>
            <dd>
              <Fingerprint value={ca.fingerprint_sha1} testId="ca-fingerprint-sha1" />
              <p className="mt-1 text-muted-foreground">{t("settings.certificate.ca.sha1Hint")}</p>
            </dd>
          </dl>
          <a
            href={CA_CERTIFICATE_URL}
            download="twinraid-ca.crt"
            className={buttonVariants({ variant: "default", className: "self-start" })}
          >
            <Download aria-hidden="true" />
            {t("settings.certificate.ca.download")}
          </a>
        </section>
      ) : (
        <p className="text-sm text-muted-foreground">{t("settings.certificate.customHint")}</p>
      )}
      <section className="flex flex-col gap-3" aria-labelledby="server-heading">
        <h2 id="server-heading" className="font-semibold">
          {t("settings.certificate.server.title")}
        </h2>
        <dl className="grid gap-3 text-sm sm:grid-cols-[max-content_1fr] sm:gap-x-6">
          <dt className="font-medium">{t("settings.certificate.source")}</dt>
          <dd>{t(`settings.certificate.sources.${tls.source}`)}</dd>
          <dt className="font-medium">{t("settings.certificate.validUntil")}</dt>
          <dd>
            {date(server.not_after)} (
            {t("settings.certificate.daysLeft", { count: server.days_left })})
            {tls.source === "ca" && (
              <p className="mt-1 text-muted-foreground">
                {t("settings.certificate.server.renewal")}
              </p>
            )}
          </dd>
          <dt className="font-medium">{t("settings.certificate.fingerprint")}</dt>
          <dd>
            <Fingerprint value={server.fingerprint_sha256} testId="server-fingerprint" />
            <p className="mt-1 text-muted-foreground">{t("settings.certificate.compare")}</p>
          </dd>
        </dl>
      </section>
    </div>
  );
}

function ConnectionCard({ info }: { info: AppInfo }) {
  const { t } = useTranslation();
  const name = info.names.target;
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            {info.target_connection_encrypted ? (
              <Lock className="size-5" aria-hidden="true" />
            ) : (
              <LockOpen className="size-5" aria-hidden="true" />
            )}
            {t("settings.connection.title", { name })}
          </CardTitle>
          <CardDescription>{t("settings.connection.description")}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {info.target_connection_encrypted ? (
          <p className="text-sm" data-testid="connection-encrypted">
            {t("settings.connection.encrypted")}
          </p>
        ) : (
          <ErrorNotice message={t("settings.connection.unencrypted", { name })} />
        )}
      </CardContent>
    </Card>
  );
}

export function SettingsPage({ info, tls, onBack }: SettingsPageProps) {
  const { t } = useTranslation();
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
        <CardContent>
          <CertificateCard tls={tls} />
        </CardContent>
      </Card>
      {info !== null && <ConnectionCard info={info} />}
    </div>
  );
}
