import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface PlaceholderTileProps {
  title: string;
  description: string;
  empty: string;
  className?: string;
}

export function PlaceholderTile({ title, description, empty, className }: PlaceholderTileProps) {
  return (
    <Card className={className}>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground italic">{empty}</p>
      </CardContent>
    </Card>
  );
}
