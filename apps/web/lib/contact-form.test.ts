import { describe, expect, it } from "vitest";

import {
  EMPTY_CONTACT_FORM,
  TEAM_SIZES,
  buildContactEmail,
  type ContactFormValues,
  hasErrors,
  isConsumerEmail,
  validateContactForm,
} from "./contact-form";

const valid: ContactFormValues = {
  name: "Dana Okonkwo",
  email: "dana@acme.co",
  company: "Acme",
  teamSize: "11-50",
  message: "We close the books manually every month and want to automate the vendor invoices.",
};

describe("validateContactForm", () => {
  it("accepts a complete submission", () => {
    expect(hasErrors(validateContactForm(valid))).toBe(false);
  });

  it("reports every missing required field at once", () => {
    // One round-trip should surface all problems, not the first one.
    const errors = validateContactForm(EMPTY_CONTACT_FORM);
    expect(Object.keys(errors).sort()).toEqual(["email", "message", "name", "teamSize"]);
  });

  it("does not require a company", () => {
    expect(validateContactForm({ ...valid, company: "" }).company).toBeUndefined();
  });

  it("rejects an address with no domain", () => {
    expect(validateContactForm({ ...valid, email: "dana@acme" }).email).toBeDefined();
    expect(validateContactForm({ ...valid, email: "dana" }).email).toBeDefined();
  });

  it("accepts plus-tagged and multi-level domains", () => {
    // Common false rejections from over-strict email patterns.
    expect(validateContactForm({ ...valid, email: "dana+orkest@acme.co.uk" }).email).toBeUndefined();
    expect(validateContactForm({ ...valid, email: "d@mail.corp.acme.io" }).email).toBeUndefined();
  });

  it("rejects a team size that is not on the list", () => {
    expect(validateContactForm({ ...valid, teamSize: "12" }).teamSize).toBeDefined();
  });

  it("accepts every offered team size", () => {
    for (const size of TEAM_SIZES) {
      expect(validateContactForm({ ...valid, teamSize: size.value }).teamSize).toBeUndefined();
    }
  });

  it("rejects a message too short to route", () => {
    expect(validateContactForm({ ...valid, message: "hi" }).message).toBeDefined();
  });

  it("treats whitespace-only input as empty", () => {
    const errors = validateContactForm({ ...valid, name: "   ", message: "        " });
    expect(errors.name).toBeDefined();
    expect(errors.message).toBeDefined();
  });

  it("bounds the fields it stores", () => {
    expect(validateContactForm({ ...valid, name: "a".repeat(121) }).name).toBeDefined();
    expect(validateContactForm({ ...valid, message: "a".repeat(4001) }).message).toBeDefined();
    expect(validateContactForm({ ...valid, company: "a".repeat(161) }).company).toBeDefined();
  });
});

describe("isConsumerEmail", () => {
  it("recognises free-mail domains regardless of case", () => {
    expect(isConsumerEmail("someone@GMAIL.com")).toBe(true);
    expect(isConsumerEmail("someone@icloud.com")).toBe(true);
  });

  it("treats company domains as business addresses", () => {
    expect(isConsumerEmail("dana@acme.co")).toBe(false);
  });

  it("does not throw on a malformed address", () => {
    expect(isConsumerEmail("nope")).toBe(false);
    expect(isConsumerEmail("")).toBe(false);
  });
});

describe("buildContactEmail", () => {
  const base = { ...EMPTY_CONTACT_FORM, name: "Ada Lovelace", email: "ada@acme.test", teamSize: "11-50", message: "We close the books by hand." };

  it("puts the visitor in Reply-To so answering the email answers the lead", () => {
    expect(buildContactEmail(base).replyTo).toBe("ada@acme.test");
  });

  it("strips line breaks from anything that reaches a header", () => {
    const email = buildContactEmail({ ...base, name: "Ada\r\nBcc: attacker@evil.test", company: "Acme\nCo" });
    expect(email.subject).not.toMatch(/[\r\n]/);
    expect(email.replyTo).not.toMatch(/[\r\n]/);
  });

  it("shows the human label for the team size and keeps the message intact", () => {
    const email = buildContactEmail(base);
    expect(email.text).toContain("Team size: 11–50 people");
    expect(email.text.endsWith("We close the books by hand.")).toBe(true);
  });

  it("omits the company from the subject when none was given", () => {
    expect(buildContactEmail(base).subject).toBe("Orkest enquiry: Ada Lovelace");
    expect(buildContactEmail({ ...base, company: "Acme" }).subject).toBe("Orkest enquiry: Ada Lovelace (Acme)");
  });

  it("caps the subject length", () => {
    expect(buildContactEmail({ ...base, name: "x".repeat(500) }).subject.length).toBeLessThanOrEqual(200);
  });
});
