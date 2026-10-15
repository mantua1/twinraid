import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ResourceInfo, StatusReason } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// Ursachen, die nur informieren; alle anderen zeigen ein Problem an.
const NEUTRAL_REASONS = new Set<StatusReason>([
  "runs_here",
  "target_offline",
  "target_starting",
  "vm_paused",
  "vm_suspended",
]);

interface StatusTileProps {
  title: string;
  description: string;
  info: ResourceInfo;
  // Aktionen und ihr Ergebnis (Konzept 4.1), z. B. für Groß und die VM.
  children?: ReactNode;
  testId?: string;
}

export function StatusTile({ title, description, info, children, testId }: StatusTileProps) {
  const { t } = useTranslation();
  const { reason } = info;
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        <StatusBadge state={info.state} />
      </CardHeader>
      {(reason !== null || children !== undefined) && (
        <CardContent className="flex flex-col gap-3">
          {reason !== null && (
            <p
              data-testid="status-reason"
              className={cn(
                "text-sm",
                NEUTRAL_REASONS.has(reason) ? "text-muted-foreground" : "text-destructive",
              )}
            >
              {t(`status.reasons.${reason}`)}
            </p>
          )}
          {children}
        </CardContent>
      )}
    </Card>
  );
}
