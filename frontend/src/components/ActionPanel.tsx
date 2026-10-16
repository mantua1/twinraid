import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { ActionBlocker, ActionName, ActionsSnapshot } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import type { ActionRequestResult } from "@/hooks/useDashboardData";

export interface PanelAction {
  name: ActionName;
  label: string;
  variant?: "default" | "outline" | "destructive";
  // Kurzer Zusatz unter den Buttons, z. B. „gültig bis 20:45“.
  note?: string;
  confirm?: {
    title: string;
    description: string;
    confirmLabel: string;
    destructive?: boolean;
  };
}

interface ActionPanelProps {
  actions: PanelAction[];
  snapshot: ActionsSnapshot | null;
  onRun: (name: ActionName) => Promise<ActionRequestResult>;
}

// Gründe, die nicht schon aus dem Status hervorgehen, werden unter den Buttons genannt.
const EXPLAINED = new Set<ActionBlocker>([
  "not_enabled",
  "sync_running",
  "target_unknown",
  "vm_unknown",
  "force_not_yet",
  "tour",
]);

/** Buttons einer Kachel. Verfügbar ist nur, was der Worker als möglich meldet (Konzept 4.2). */
export function ActionPanel({ actions, snapshot, onRun }: ActionPanelProps) {
  const { t, i18n } = useTranslation();
  const [pending, setPending] = useState<ActionName | null>(null);
  // Stand beim Absenden einer angenommenen Aktion: Bis der Live-Status ihn ersetzt,
  // bleiben die Buttons gesperrt (sonst wären sie bis zu 2 s lang wieder klickbar).
  const [sentWith, setSentWith] = useState<ActionsSnapshot | null>(null);
  const [confirming, setConfirming] = useState<PanelAction | null>(null);
  // Meldung zur letzten Anfrage, gültig nur für den Stand, bei dem sie entstand: Mit dem
  // nächsten Live-Status verschwindet sie (Konzept 4.2, ab v0.4.1).
  const [message, setMessage] = useState<{ text: string; with: ActionsSnapshot | null } | null>(
    null,
  );

  const waiting = pending !== null || (sentWith !== null && sentWith === snapshot);
  const blockerOf = (name: ActionName): ActionBlocker | null =>
    snapshot === null ? "target_unknown" : snapshot.availability[name];

  const run = (name: ActionName) => {
    const current = snapshot;
    setPending(name);
    setMessage(null);
    void onRun(name)
      .then((result) => {
        setSentWith(result.kind === "accepted" ? current : null);
        if (result.kind === "rejected") {
          const reason =
            result.reason === "rejected"
              ? t("actions.rejectedUnknown")
              : t(`actions.blockers.${result.reason}`);
          setMessage({ text: t("actions.rejected", { reason }), with: current });
        } else if (result.kind === "failed") {
          setMessage({ text: t("actions.requestFailed"), with: current });
        }
      })
      .finally(() => {
        setPending(null);
      });
  };

  const hint = (blocker: ActionBlocker): string => {
    if (blocker === "force_not_yet" && snapshot?.force_available_at != null) {
      const time = new Intl.DateTimeFormat(i18n.resolvedLanguage, { timeStyle: "medium" }).format(
        new Date(snapshot.force_available_at),
      );
      return t("actions.forceAt", { time });
    }
    return t(`actions.blockers.${blocker}`);
  };

  // Gleiche Gründe zusammenfassen: „Wecken, Herunterfahren: nicht freigeschaltet“.
  // Vor dem ersten Live-Status gibt es keine Hinweise; die Buttons sind dann gesperrt.
  const hints = new Map<string, string[]>();
  for (const action of snapshot === null ? [] : actions) {
    const blocker = blockerOf(action.name);
    if (blocker !== null && EXPLAINED.has(blocker)) {
      const text = hint(blocker);
      hints.set(text, [...(hints.get(text) ?? []), action.label]);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {actions.map((action) => (
          <Button
            key={action.name}
            size="sm"
            variant={action.variant ?? "outline"}
            disabled={waiting || blockerOf(action.name) !== null}
            data-action={action.name}
            onClick={() => {
              if (action.confirm) {
                setConfirming(action);
              } else {
                run(action.name);
              }
            }}
          >
            {action.label}
          </Button>
        ))}
      </div>
      {actions
        .filter((action) => action.note !== undefined)
        .map((action) => (
          <p key={action.name} className="text-xs text-muted-foreground" data-testid="action-note">
            {t("actions.hint", { actions: action.label, reason: action.note })}
          </p>
        ))}
      {[...hints].map(([text, labels]) => (
        <p key={text} className="text-xs text-muted-foreground" data-testid="action-hint">
          {t("actions.hint", { actions: labels.join(", "), reason: text })}
        </p>
      ))}
      {message !== null && message.with === snapshot && (
        <p role="alert" className="text-sm text-destructive">
          {message.text}
        </p>
      )}
      {confirming?.confirm && (
        <ConfirmDialog
          title={confirming.confirm.title}
          description={confirming.confirm.description}
          confirmLabel={confirming.confirm.confirmLabel}
          destructive={confirming.confirm.destructive}
          onCancel={() => {
            setConfirming(null);
          }}
          onConfirm={() => {
            setConfirming(null);
            run(confirming.name);
          }}
        />
      )}
    </div>
  );
}
