import { useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

interface ConfirmDialogProps {
  title: string;
  description: string;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Bestätigung vor störenden Aktionen (Konzept 4.1). Natives <dialog> als Modal:
 * keine zusätzliche Bibliothek, Escape bricht ab.
 */
export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
    return () => {
      dialog?.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
      className="m-auto w-[calc(100%-2rem)] max-w-md rounded-xl border bg-card p-6 text-card-foreground shadow-lg backdrop:bg-black/50"
    >
      <h2 id={titleId} className="text-lg font-semibold">
        {title}
      </h2>
      <p id={descriptionId} className="mt-3 text-sm text-muted-foreground">
        {description}
      </p>
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <Button variant="outline" onClick={onCancel}>
          {t("actions.dialog.cancel")}
        </Button>
        <Button variant={destructive ? "destructive" : "default"} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </div>
    </dialog>
  );
}
