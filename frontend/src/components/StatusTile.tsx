import type { ResourceState } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface StatusTileProps {
  title: string;
  description: string;
  state: ResourceState;
}

export function StatusTile({ title, description, state }: StatusTileProps) {
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        <StatusBadge state={state} />
      </CardHeader>
    </Card>
  );
}
