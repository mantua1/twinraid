import { useTranslation } from "react-i18next";

import type { ResourceInfo, StatusReason } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// Ursachen, die nur informieren; alle anderen zeigen ein Problem an.
const NEUTRAL_REASONS = new Set<StatusReason>([
  "runs_here",
  "target_offline",
  "vm_paused",
  "vm_suspended",
]);

interface StatusTileProps {
  title: string;
  description: string;
  info: ResourceInfo;
}

export function StatusTile({ title, description, info }: StatusTileProps) {
  const { t } = useTranslation();
  const { reason } = info;
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        <StatusBadge state={info.state} />
      </CardHeader>
      {reason !== null && (
        <CardContent>
          <p
            data-testid="status-reason"
            className={cn(
              "text-sm",
              NEUTRAL_REASONS.has(reason) ? "text-muted-foreground" : "text-destructive",
            )}
          >
            {t(`status.reasons.${reason}`)}
          </p>
        </CardContent>
      )}
    </Card>
  );
}
