import { useTranslation } from "react-i18next";

import type { GuardCheckMirror, GuardDataset, StatusSnapshot } from "@/api/types";
import { formatSince } from "@/lib/format";

interface SyncSnapshotsProps {
  status: StatusSnapshot;
  // Namen von Klein und Groß für die Texte.
  names: { source: string; target: string };
}

function SourceSide({
  dataset,
  newest,
  keepDays,
  language,
  name,
}: {
  dataset: GuardDataset | undefined;
  // Zeitpunkt des Snapshots im Job, solange der Job im Status steht.
  newest: string | null;
  keepDays: number | null;
  language: string | undefined;
  name: string;
}) {
  const { t } = useTranslation();
  const time = (value: string) => formatSince(language, value);
  const lines: string[] = [];
  if (dataset?.snapshots != null) {
    lines.push(t("sync.snapshots.count", { count: dataset.snapshots }));
    if (newest !== null) {
      lines.push(t("sync.snapshots.newest", { time: time(newest) }));
    }
    if (dataset.oldest_snapshot_at !== null) {
      lines.push(t("sync.snapshots.oldest", { time: time(dataset.oldest_snapshot_at) }));
    }
    lines.push(
      dataset.base_created_at !== null
        ? t("sync.snapshots.base", { time: time(dataset.base_created_at) })
        : t("sync.snapshots.noBase"),
    );
    lines.push(
      t(dataset.bookmark === true ? "sync.snapshots.bookmark" : "sync.snapshots.noBookmark"),
    );
    if (keepDays !== null) {
      lines.push(t("sync.snapshots.keep", { days: keepDays }));
    }
  }
  return (
    <section data-testid="snapshots-source">
      <h4 className="font-medium">{t("sync.snapshots.sourceHeading", { source: name })}</h4>
      {lines.length === 0 ? (
        <p className="text-muted-foreground">{t("sync.snapshots.sourceUnknown")}</p>
      ) : (
        <ul>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function TargetSide({
  mirror,
  keepDays,
  checkedAt,
  language,
  name,
}: {
  // null: Groß hat noch nicht geantwortet oder kennt den Share nicht.
  mirror: GuardCheckMirror | null;
  keepDays: number | null;
  checkedAt: string | null;
  language: string | undefined;
  name: string;
}) {
  const { t } = useTranslation();
  const time = (value: string) => formatSince(language, value);
  let content: string[];
  if (mirror === null) {
    content = [t("sync.snapshots.unknown")];
  } else if (mirror.state === "absent") {
    content = [t("sync.snapshots.absent")];
  } else if (mirror.state === "foreign") {
    content = [t("sync.snapshots.foreign")];
  } else if (mirror.props === "mismatch") {
    content = [t("sync.snapshots.locked")];
  } else {
    content = [];
    if (mirror.count !== null) {
      content.push(t("sync.snapshots.count", { count: mirror.count }));
    }
    if (mirror.newest_received !== null) {
      content.push(t("sync.snapshots.newestReceived", { time: time(mirror.newest_received) }));
    }
    if (mirror.newest_created !== null) {
      content.push(t("sync.snapshots.newestCreated", { time: time(mirror.newest_created) }));
    }
    if (mirror.oldest_created !== null) {
      content.push(t("sync.snapshots.oldest", { time: time(mirror.oldest_created) }));
    }
    if (keepDays !== null) {
      content.push(t("sync.snapshots.keep", { days: keepDays }));
    }
  }
  return (
    <section data-testid="snapshots-target">
      <h4 className="font-medium">{t("sync.snapshots.targetHeading", { target: name })}</h4>
      <ul>
        {content.map((line) => (
          <li key={line}>{line}</li>
        ))}
        {mirror !== null && checkedAt !== null && (
          <li className="text-muted-foreground">
            {t("sync.snapshots.checkedAt", { time: time(checkedAt) })}
          </li>
        )}
      </ul>
    </section>
  );
}

/**
 * Snapshots beider Seiten je Share, aufklappbar (Konzept 4.1, „Kachel Sync ab v0.5.3“).
 * Alle Werte stammen aus dem frischen Status des Wächters; es gibt dafür kein eigenes Feld.
 */
export function SyncSnapshots({ status, names }: SyncSnapshotsProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const details = status.source_guard.details;
  const shares = status.sync.levels.configured;
  if (shares.length === 0) {
    return null;
  }
  const check = details?.target_check ?? null;
  const answered = check !== null && check.result === "ok";
  return (
    <div className="flex flex-col gap-1" data-testid="sync-snapshots">
      {shares.map((share) => {
        const job = details?.job ?? null;
        const newest = job?.datasets.some((dataset) => dataset.name === share)
          ? job.created_at
          : null;
        return (
          <details key={share} className="text-sm" data-testid="snapshots-share">
            <summary className="cursor-pointer font-medium">
              {t("sync.snapshots.title", { share })}
            </summary>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <SourceSide
                dataset={details?.zfs.datasets.find((dataset) => dataset.name === share)}
                newest={newest}
                keepDays={details?.limits.snapshot_keep_days ?? null}
                language={language}
                name={names.source}
              />
              <TargetSide
                mirror={
                  answered ? (check.mirrors.find((item) => item.name === share) ?? null) : null
                }
                keepDays={check?.summary?.keep_days ?? null}
                checkedAt={check?.at ?? null}
                language={language}
                name={names.target}
              />
            </div>
          </details>
        );
      })}
      <p className="text-xs text-muted-foreground">{t("sync.snapshots.restore")}</p>
    </div>
  );
}
