import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { type ThemeChoice, useTheme } from "@/hooks/useTheme";

const OPTIONS: { choice: ThemeChoice; icon: LucideIcon }[] = [
  { choice: "light", icon: Sun },
  { choice: "dark", icon: Moon },
  { choice: "system", icon: Monitor },
];

export function ThemeToggle() {
  const { t } = useTranslation();
  const [current, setTheme] = useTheme();
  return (
    <div role="group" aria-label={t("theme.label")} className="flex rounded-md border p-0.5">
      {OPTIONS.map(({ choice, icon: Icon }) => (
        <Button
          key={choice}
          variant={current === choice ? "secondary" : "ghost"}
          size="icon"
          className="size-8"
          aria-pressed={current === choice}
          aria-label={t(`theme.${choice}`)}
          title={t(`theme.${choice}`)}
          onClick={() => {
            setTheme(choice);
          }}
        >
          <Icon aria-hidden="true" />
        </Button>
      ))}
    </div>
  );
}
