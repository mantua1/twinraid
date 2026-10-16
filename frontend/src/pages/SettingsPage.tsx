import { ArrowLeft, Download, KeyRound, Power, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CA_CERTIFICATE_URL } from "@/api/client";
import type { AppInfo, StatusNotice, TlsInfo } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { NoticeList } from "@/components/NoticeList";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface SettingsPageProps {
  info: AppInfo | null;
  tls: TlsInfo | null;
  // Hinweise aus dem Live-Status, z. B. zu Unraid Connect auf Groß.
  notices: readonly StatusNotice[];
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
          <dt className="font-medium">{t("settings.certificate.address.label")}</dt>
          <dd data-testid="tls-address">
            {[...tls.addresses.ip_addresses, ...tls.addresses.hostnames].join(", ")}
            <p className="mt-1 text-muted-foreground">
              {tls.addresses.origin === "detected"
                ? t("settings.certificate.address.detected", {
                    interface: tls.addresses.interface ?? "?",
                  })
                : t("settings.certificate.address.configured")}
            </p>
          </dd>
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

/** Wie TwinRaid mit Groß spricht: SSH über guard.sh, ohne API-Key (Konzept 3.5). */
function ConnectionCard({ info, notices }: { info: AppInfo; notices: readonly StatusNotice[] }) {
  const { t } = useTranslation();
  const name = info.names.target;
  const targetNotices = notices.filter((notice) => notice.startsWith("target_"));
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="size-5" aria-hidden="true" />
            {t("settings.connection.title", { name })}
          </CardTitle>
          <CardDescription>{t("settings.connection.description")}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm" data-testid="connection-ssh">
          {t("settings.connection.ssh", { name })}
        </p>
        <p className="text-sm text-muted-foreground">{t("settings.connection.webgui")}</p>
        <NoticeList notices={targetNotices} target={name} />
      </CardContent>
    </Card>
  );
}

const ACTION_GROUPS = ["wake", "vm", "shutdown"] as const;

/** Welche Aktionen im Modus real freigeschaltet sind (Konzept 4.2). */
function ActionsCard({ info }: { info: AppInfo }) {
  const { t } = useTranslation();
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <Power className="size-5" aria-hidden="true" />
            {t("settings.actions.title")}
          </CardTitle>
          <CardDescription>
            {info.mode === "mock" ? t("settings.actions.mock") : t("settings.actions.description")}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2 text-sm">
          {ACTION_GROUPS.map((group) => (
            <div key={group} className="contents">
              <dt className="font-medium">{t(`settings.actions.groups.${group}`)}</dt>
              <dd data-testid={`action-group-${group}`}>
                {info.actions[group]
                  ? t("settings.actions.enabled")
                  : t("settings.actions.disabled")}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export function SettingsPage({ info, tls, notices, onBack }: SettingsPageProps) {
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
      {info !== null && <ConnectionCard info={info} notices={notices} />}
      {info !== null && <ActionsCard info={info} />}
    </div>
  );
}
