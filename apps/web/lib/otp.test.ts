import { describe, expect, it } from "vitest";

import { isCompleteCode, maskEmail, resendLabel, sanitizeCode, secondsUntil } from "./otp";

describe("sanitizeCode", () => {
  it("keeps digits only", () => {
    expect(sanitizeCode("48 29-13")).toBe("482913");
    expect(sanitizeCode("abc")).toBe("");
  });

  it("caps at six, so a pasted longer string cannot overflow the input", () => {
    expect(sanitizeCode("12345678")).toBe("123456");
  });
});

describe("isCompleteCode", () => {
  it("accepts exactly six digits", () => {
    expect(isCompleteCode("482913")).toBe(true);
    expect(isCompleteCode("000000")).toBe(true);
  });

  it("rejects anything shorter, longer or non-numeric", () => {
    expect(isCompleteCode("48291")).toBe(false);
    expect(isCompleteCode("4829133")).toBe(false);
    expect(isCompleteCode("48291a")).toBe(false);
    expect(isCompleteCode("")).toBe(false);
  });
});

describe("resendLabel / secondsUntil", () => {
  it("counts down, then offers the resend", () => {
    expect(resendLabel(42)).toBe("Resend in 42s");
    expect(resendLabel(0)).toBe("Resend code");
  });

  it("rounds a partial second up and never goes negative", () => {
    expect(secondsUntil(10_500, 10_000)).toBe(1);
    expect(secondsUntil(10_000, 10_000)).toBe(0);
    expect(secondsUntil(9_000, 10_000)).toBe(0);
  });
});

describe("maskEmail", () => {
  it("hides most of the local part and keeps the domain", () => {
    expect(maskEmail("pat@example.com")).toBe("pa••@example.com");
    expect(maskEmail("patricia.example@acme.test")).toMatch(/^pa•+@acme\.test$/);
  });

  it("never returns the whole local part", () => {
    expect(maskEmail("ab@x.io")).not.toContain("ab@");
  });

  it("leaves something that is not an address alone", () => {
    expect(maskEmail("not-an-email")).toBe("not-an-email");
  });
});
