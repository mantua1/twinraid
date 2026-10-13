import { TriangleAlert } from "lucide-react";

export function ErrorNotice({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive"
    >
      <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
