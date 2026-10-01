import { describe, expect, it } from "vitest";

import { authErrorMessage, isEmailNotVerified } from "./auth-errors";

describe("authErrorMessage", () => {
  it.each(["google_cancelled", "google_unverified", "google_conflict", "google_no_org", "google_state", "google_failed"])(
    "has words for %s",
    (slug) => {
      expect(authErrorMessage(slug)).toEqual(expect.any(String));
    },
  );

  it("shows nothing for an unknown slug, so junk in the URL renders nothing", () => {
    expect(authErrorMessage("<script>alert(1)</script>")).toBeNull();
    expect(authErrorMessage("toString")).toBeNull();
    expect(authErrorMessage("__proto__")).toBeNull();
  });

  it("shows nothing when there is no error", () => {
    expect(authErrorMessage(null)).toBeNull();
    expect(authErrorMessage(undefined)).toBeNull();
    expect(authErrorMessage("")).toBeNull();
  });
});

describe("isEmailNotVerified", () => {
  const axiosError = (status: number, detail: unknown) => ({ isAxiosError: true, response: { status, data: { detail } } });

  it("recognises the API's refusal", () => {
    expect(isEmailNotVerified(axiosError(403, "email_not_verified"))).toBe(true);
  });

  it("is not triggered by other 403s, other statuses or non-axios errors", () => {
    expect(isEmailNotVerified(axiosError(403, "User does not belong to any active organizations"))).toBe(false);
    expect(isEmailNotVerified(axiosError(401, "email_not_verified"))).toBe(false);
    expect(isEmailNotVerified(new Error("email_not_verified"))).toBe(false);
    expect(isEmailNotVerified(null)).toBe(false);
  });
});
