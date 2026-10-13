import { useTranslation } from "react-i18next";

import type { ResourceState } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATE_STYLES: Record<ResourceState, string> = {
  online: "border-emerald-600/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  offline: "border-zinc-500/40 bg-zinc-500/15 text-zinc-700 dark:text-zinc-300",
  starting: "border-sky-600/40 bg-sky-500/15 text-sky-700 dark:text-sky-300",
  stopping: "border-amber-600/40 bg-amber-500/15 text-amber-800 dark:text-amber-300",
  unknown: "border-border bg-muted text-muted-foreground",
};

export function StatusBadge({ state }: { state: ResourceState }) {
  const { t } = useTranslation();
  return (
    <Badge variant="outline" data-state={state} className={cn(STATE_STYLES[state])}>
      {t(`status.${state}`)}
    </Badge>
  );
}
