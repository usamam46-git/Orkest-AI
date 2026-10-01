/**
 * Small pure helpers for the email-verification screen.
 *
 * Kept out of the component so the rules that decide whether a code is submittable
 * and how long the resend button stays disabled are asserted, not eyeballed.
 */

export const OTP_LENGTH = 6;

/** Seconds the resend button stays disabled. Matches the API's per-address cooldown. */
export const RESEND_COOLDOWN_SECONDS = 60;

/** Keep digits only, capped at the code length — what a paste or autofill might contain is not trusted. */
export function sanitizeCode(raw: string): string {
  return raw.replace(/\D/g, "").slice(0, OTP_LENGTH);
}

export function isCompleteCode(code: string): boolean {
  return new RegExp(`^\\d{${OTP_LENGTH}}$`).test(code);
}

/** "Resend code" once the wait is over, "Resend in 42s" while it is not. */
export function resendLabel(secondsLeft: number): string {
  return secondsLeft > 0 ? `Resend in ${secondsLeft}s` : "Resend code";
}

/** Whole seconds remaining until `readyAt` (ms epoch), never negative. */
export function secondsUntil(readyAt: number, now: number): number {
  return Math.max(0, Math.ceil((readyAt - now) / 1000));
}

/**
 * Hide most of an address for display ("pa***@example.com"). The screen shows where
 * the code went; it does not need to repeat a full address back to whoever is
 * looking over a shoulder.
 */
export function maskEmail(email: string): string {
  const at = email.lastIndexOf("@");
  if (at < 1) return email;
  const local = email.slice(0, at);
  const keep = Math.min(2, Math.max(1, local.length - 1));
  return `${local.slice(0, keep)}${"•".repeat(Math.max(2, local.length - keep))}${email.slice(at)}`;
}
