// Zeiten und Größen für die Anzeige, in der gewählten Sprache.

/** Uhrzeit hh:mm oder hh:mm:ss. */
export function formatTime(language: string | undefined, value: Date | string, seconds = false) {
  return new Intl.DateTimeFormat(language, { timeStyle: seconds ? "medium" : "short" }).format(
    new Date(value),
  );
}

/** Uhrzeit mit Sekunden; liegt der Zeitpunkt nicht heute, mit Datum. */
export function formatSince(language: string | undefined, value: string, now = new Date()) {
  const date = new Date(value);
  if (date.toDateString() === now.toDateString()) {
    return formatTime(language, date, true);
  }
  return new Intl.DateTimeFormat(language, { dateStyle: "short", timeStyle: "medium" }).format(
    date,
  );
}

/** Auf die volle Minute aufgerundet, damit „frühestens um hh:mm“ nie zu früh ist. */
export function roundUpToMinute(value: string): Date {
  const minute = 60_000;
  return new Date(Math.ceil(new Date(value).getTime() / minute) * minute);
}

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/** Größe in Byte mit Einheit zur Basis 1024, eine Nachkommastelle. */
export function formatBytes(language: string | undefined, bytes: number) {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const number = new Intl.NumberFormat(language, {
    maximumFractionDigits: unit === 0 ? 0 : 1,
  }).format(value);
  return `${number} ${UNITS[unit] ?? "B"}`;
}
