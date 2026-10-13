import { KeyRound, LogIn } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, LoginError } from "@/api/client";
import type { LoginErrorCode } from "@/api/types";
import { ErrorNotice } from "@/components/ErrorNotice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

interface LoginPageProps {
  passwordSet: boolean | null;
  sessionExpired: boolean;
  onLoggedIn: () => void;
}

interface LoginFailure {
  code: LoginErrorCode;
  retryAfterSeconds: number | null;
}

export function LoginPage({ passwordSet, sessionExpired, onLoggedIn }: LoginPageProps) {
  const { t } = useTranslation();
  const inputId = useId();
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<LoginFailure | null>(null);
  const showNoPasswordHint = passwordSet === false || failure?.code === "password_not_set";

  async function submit() {
    setSubmitting(true);
    setFailure(null);
    try {
      await api.login(password);
      setPassword("");
      onLoggedIn();
    } catch (error) {
      const login = error instanceof LoginError ? error : new LoginError("network");
      setFailure({ code: login.code, retryAfterSeconds: login.retryAfterSeconds });
    } finally {
      setSubmitting(false);
    }
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    void submit();
  }

  function failureText(item: LoginFailure): string {
    const seconds = item.retryAfterSeconds ?? 0;
    switch (item.code) {
      case "locked":
        return t("login.errors.locked", { minutes: Math.max(1, Math.ceil(seconds / 60)) });
      case "busy":
        return t("login.errors.busy", { seconds: Math.max(1, seconds) });
      default:
        return t(`login.errors.${item.code}`);
    }
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 px-4 py-10">
      {sessionExpired && <ErrorNotice message={t("errors.sessionExpired")} />}
      {showNoPasswordHint && (
        <div
          role="note"
          className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm"
        >
          <p className="flex items-center gap-2 font-semibold">
            <KeyRound className="size-4" aria-hidden="true" />
            {t("login.noPassword.title")}
          </p>
          <p className="mt-1">{t("login.noPassword.text")}</p>
          <code className="mt-2 block rounded bg-muted px-2 py-1 font-mono text-xs">
            {t("login.noPassword.command")}
          </code>
        </div>
      )}
      <Card>
        <CardHeader>
          <div>
            <CardTitle>{t("login.title")}</CardTitle>
            <CardDescription>{t("login.description")}</CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-3" onSubmit={onSubmit} noValidate>
            <label htmlFor={inputId} className="text-sm font-medium">
              {t("login.password")}
            </label>
            <Input
              id={inputId}
              type="password"
              autoComplete="current-password"
              autoFocus
              required
              value={password}
              aria-invalid={failure !== null}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
            {failure !== null && failure.code !== "password_not_set" && (
              <ErrorNotice message={failureText(failure)} />
            )}
            <Button type="submit" disabled={submitting || password === ""}>
              <LogIn aria-hidden="true" />
              {submitting ? t("login.submitting") : t("login.submit")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
