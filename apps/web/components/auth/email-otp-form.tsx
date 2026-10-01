"use client";

import * as React from "react";
import Link from "next/link";
import { Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { InputOTP, InputOTPGroup, InputOTPSeparator, InputOTPSlot } from "@/components/ui/input-otp";
import { Label } from "@/components/ui/label";
import { authApi } from "@/lib/api";
import { getApiErrorMessage } from "@/lib/api-client";
import { OTP_LENGTH, RESEND_COOLDOWN_SECONDS, isCompleteCode, maskEmail, resendLabel, sanitizeCode, secondsUntil } from "@/lib/otp";

/**
 * "Verify your email" — the step between submitting the sign-up form and being let in.
 *
 * Nothing is signed in until this succeeds: the API issues no session at sign-up
 * when verification is required, and `verifyEmail` is what returns the first token.
 * The same screen serves three entrances (register, login-with-an-unverified-
 * account, accept-invite), so it owns nothing about where to go next — it calls
 * `onVerified` with the access token and the caller decides.
 *
 * Behaviour worth knowing:
 *
 * - **Submits itself at the sixth digit** (`onComplete`), with the button as the
 *   fallback for anyone who pastes and wants to confirm. A guard stops the two
 *   firing for the same code.
 * - **The resend button starts disabled** when a code was sent a moment ago, which
 *   is the case on every entrance: sign-up and the unverified-login refusal both
 *   mail a code before this renders. The 60 s matches the API's per-address
 *   cooldown, so the button is never enabled when the server would ignore it.
 * - **Any failure clears the boxes.** A wrong code is not worth editing digit by
 *   digit, and the API burns a code after five misses anyway.
 */
export function EmailOtpForm({
  email,
  onVerified,
  onBack,
  codeJustSent = true,
}: {
  email: string;
  onVerified: (accessToken: string) => void;
  onBack: () => void;
  /** True when this screen opens straight after the API mailed a code. */
  codeJustSent?: boolean;
}) {
  const [code, setCode] = React.useState("");
  const [pending, setPending] = React.useState(false);
  const [resending, setResending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const submitting = React.useRef(false);

  const [readyAt, setReadyAt] = React.useState(() => (codeJustSent ? Date.now() + RESEND_COOLDOWN_SECONDS * 1000 : 0));
  const [now, setNow] = React.useState(() => Date.now());
  const secondsLeft = secondsUntil(readyAt, now);

  React.useEffect(() => {
    if (secondsLeft <= 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [secondsLeft]);

  async function verify(value: string) {
    if (!isCompleteCode(value) || submitting.current) return;
    submitting.current = true;
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      const response = await authApi.verifyEmail({ email, code: value });
      onVerified(response.access_token);
    } catch (failure) {
      setError(getApiErrorMessage(failure, "That code is invalid or has expired."));
      setCode("");
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  async function resend() {
    if (secondsLeft > 0 || resending) return;
    setResending(true);
    setError(null);
    try {
      await authApi.resendVerification(email);
      setReadyAt(Date.now() + RESEND_COOLDOWN_SECONDS * 1000);
      setNow(Date.now());
      setCode("");
      setNotice("If that address has a pending sign-up, a new code is on its way.");
    } catch (failure) {
      setError(getApiErrorMessage(failure, "We could not send a new code. Try again in a moment."));
    } finally {
      setResending(false);
    }
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">Verify your email</h1>
        <p className="mt-1.5 text-sm text-muted-foreground">
          Enter the {OTP_LENGTH}-digit code we sent to <span className="font-medium text-foreground">{maskEmail(email)}</span>. It
          expires in 10 minutes.
        </p>
      </div>

      <form
        className="grid gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          void verify(code);
        }}
        noValidate
      >
        {error ? (
          <div className="rounded-xl bg-status-bad-soft px-3.5 py-2.5 text-sm text-status-bad" role="alert">
            {error}
          </div>
        ) : null}

        <div className="grid gap-2.5">
          <div className="flex items-center justify-between gap-3">
            <Label htmlFor="otp-verification">Verification code</Label>
            <Button type="button" variant="outline" size="xs" onClick={resend} disabled={secondsLeft > 0 || resending}>
              {resending ? <Loader2 className="animate-spin" /> : <RefreshCw data-icon="inline-start" />}
              {resendLabel(secondsLeft)}
            </Button>
          </div>

          <InputOTP
            id="otp-verification"
            maxLength={OTP_LENGTH}
            value={code}
            onChange={(value) => setCode(sanitizeCode(value))}
            onComplete={(value) => void verify(sanitizeCode(value))}
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="one-time-code"
            autoFocus
            disabled={pending}
          >
            <InputOTPGroup>
              <InputOTPSlot index={0} />
              <InputOTPSlot index={1} />
              <InputOTPSlot index={2} />
            </InputOTPGroup>
            <InputOTPSeparator />
            <InputOTPGroup>
              <InputOTPSlot index={3} />
              <InputOTPSlot index={4} />
              <InputOTPSlot index={5} />
            </InputOTPGroup>
          </InputOTP>

          {notice ? <p className="text-xs text-muted-foreground">{notice}</p> : null}
        </div>

        <Button type="submit" className="w-full" disabled={pending || !isCompleteCode(code)}>
          {pending ? <Loader2 className="size-4 animate-spin" /> : null}
          Verify
        </Button>

        <p className="text-center text-sm text-muted-foreground">
          Wrong address?{" "}
          <button type="button" onClick={onBack} className="font-medium text-foreground hover:underline">
            Use a different email
          </button>
        </p>
        <p className="text-center text-xs text-muted-foreground">
          Having trouble?{" "}
          <Link href="/#contact" className="underline-offset-4 hover:underline">
            Contact support
          </Link>
        </p>
      </form>
    </div>
  );
}
