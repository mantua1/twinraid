// Termine des Zeitplans für die Anzeige (Konzept 4.1 und 4.8, ab v0.6.0).

/** Zeitzone des Browsers; leer, wenn er keine nennt. */
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/**
 * Termin mit Wochentag, Datum und der tatsächlichen Uhrzeit in der Zeitzone des Plans, also
 * auch am Tag einer Zeitumstellung richtig (etwa 03:00 statt 02:30).
 */
export function formatTerm(language: string | undefined, value: string, timeZone: string): string {
  return new Intl.DateTimeFormat(language, {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  }).format(new Date(value));
}

/** Der Termin, dazu die Zeitzone des Plans, wenn sie von der des Browsers abweicht. */
export function termWithZone(
  language: string | undefined,
  value: string,
  timeZone: string,
  withZone: (term: string, zone: string) => string,
): string {
  const term = formatTerm(language, value, timeZone);
  return timeZone === browserTimeZone() ? term : withZone(term, timeZone);
}

/** Dauer in Minuten als „23 h“ oder „6 d 23 h“ (Einheiten in DE und EN gleich). */
export function formatGap(minutes: number): string {
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const rest = minutes % 60;
  const parts = [];
  if (days > 0) {
    parts.push(`${String(days)} d`);
  }
  if (hours > 0) {
    parts.push(`${String(hours)} h`);
  }
  if (rest > 0 || parts.length === 0) {
    parts.push(`${String(rest)} min`);
  }
  return parts.join(" ");
}
