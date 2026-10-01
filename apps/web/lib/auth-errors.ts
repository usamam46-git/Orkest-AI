/**
 * Messages for the `?error=` slug the API's Google callback puts on `/login`.
 *
 * The callback is a browser navigation, so it cannot return JSON — it redirects
 * with a short slug and this table turns that into words. The slugs are the
 * `GoogleSignInError.code` values in `apps/api/src/modules/auth/service.py`
 * prefixed `google_`; change them together.
 *
 * Deliberately vague where precision would help an attacker: nothing here says
 * which accounts exist or why a particular address was refused beyond what the
 * person signing in already knows.
 */
const GOOGLE_ERRORS: Record<string, string> = {
  google_cancelled: "Google sign-in was cancelled. You can try again, or use your email and password.",
  google_unverified: "Google has not verified that email address, so we cannot sign you in with it.",
  google_conflict: "That email address is already linked to a different Google account.",
  google_no_org: "That account does not belong to any active organization.",
  google_state: "That sign-in attempt expired or did not start here. Please try again.",
  google_failed: "Google sign-in did not complete. Please try again.",
};

/** The message for a slug, or null when it is not one of ours (so nothing is shown for junk in the URL). */
export function authErrorMessage(slug: string | null | undefined): string | null {
  if (!slug) return null;
  return Object.hasOwn(GOOGLE_ERRORS, slug) ? GOOGLE_ERRORS[slug] : null;
}
