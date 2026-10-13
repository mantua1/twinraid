import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { chooseLanguage, LANGUAGES } from "@/i18n";

export function LanguageSwitch() {
  const { t, i18n } = useTranslation();
  return (
    <div role="group" aria-label={t("language.label")} className="flex rounded-md border p-0.5">
      {LANGUAGES.map((language) => (
        <Button
          key={language}
          variant={i18n.resolvedLanguage === language ? "secondary" : "ghost"}
          size="sm"
          className="h-8 px-2.5"
          aria-pressed={i18n.resolvedLanguage === language}
          aria-label={t(`language.${language}`)}
          title={t(`language.${language}`)}
          onClick={() => {
            chooseLanguage(language);
          }}
        >
          {t(`language.short.${language}`)}
        </Button>
      ))}
    </div>
  );
}
