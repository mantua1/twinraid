import type { ReactNode } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface PlaceholderTileProps {
  title: string;
  description: string;
  empty: string;
  className?: string;
  // Zusätzliche Zeilen, z. B. „Nächster Sync frühestens um …“ (ab v0.5.0).
  children?: ReactNode;
  testId?: string;
}

export function PlaceholderTile({
  title,
  description,
  empty,
  className,
  children,
  testId,
}: PlaceholderTileProps) {
  return (
    <Card className={className} data-testid={testId}>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground italic">{empty}</p>
        {children}
      </CardContent>
    </Card>
  );
}
