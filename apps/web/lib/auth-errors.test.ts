import { describe, expect, it } from "vitest";

import { authErrorMessage } from "./auth-errors";

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
