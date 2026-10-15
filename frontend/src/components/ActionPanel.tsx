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
  const [confirming, setConfirming] = useState<PanelAction | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const blockerOf = (name: ActionName): ActionBlocker | null =>
    snapshot === null ? "target_unknown" : snapshot.availability[name];

  const run = (name: ActionName) => {
    setPending(name);
    setMessage(null);
    void onRun(name)
      .then((result) => {
        if (result.kind === "rejected") {
          setMessage(
            t("actions.rejected", {
              reason:
                result.reason === "rejected"
                  ? t("actions.rejectedUnknown")
                  : t(`actions.blockers.${result.reason}`),
            }),
          );
        } else if (result.kind === "failed") {
          setMessage(t("actions.requestFailed"));
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
  const hints = new Map<string, string[]>();
  for (const action of actions) {
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
            disabled={pending !== null || blockerOf(action.name) !== null}
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
      {[...hints].map(([text, labels]) => (
        <p key={text} className="text-xs text-muted-foreground" data-testid="action-hint">
          {t("actions.hint", { actions: labels.join(", "), reason: text })}
        </p>
      ))}
      {message !== null && (
        <p role="alert" className="text-sm text-destructive">
          {message}
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
