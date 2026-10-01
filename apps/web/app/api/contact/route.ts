import { NextResponse } from "next/server";

import {
  EMPTY_CONTACT_FORM,
  buildContactEmail,
  type ContactFormValues,
  hasErrors,
  validateContactForm,
} from "@/lib/contact-form";

/**
 * Contact form endpoint.
 *
 * **This deliberately fails loudly when unconfigured.** A landing-page contact
 * form that renders a cheerful "thanks, we'll be in touch" while dropping the
 * submission on the floor loses real leads silently, and nobody notices until
 * someone asks why the inbox is empty. So with no destination configured this
 * returns 503 and the form shows the visitor an email address instead.
 *
 * Two destinations, tried in this order:
 *
 * 1. **Email through Resend** when `RESEND_API_KEY`, `CONTACT_FROM_EMAIL` and
 *    `CONTACT_TO_EMAIL` are all set. The sender must be on a domain verified in
 *    Resend or the API refuses it (a 403, surfaced here as a 502). Reply-To is
 *    the visitor, so answering the email answers the lead.
 * 2. **A webhook** — `CONTACT_WEBHOOK_URL`, anything that accepts a JSON POST: a
 *    Slack incoming webhook, a Zapier/Make catch hook, an internal service.
 *
 * All of it is read at request time rather than module scope so a value can be
 * rotated without a rebuild. None of it is a `NEXT_PUBLIC_` variable: it must
 * stay server-side, or the key ships to the browser and anyone can send mail as
 * you.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Expected a JSON body." }, { status: 400 });
  }

  const raw = body as Partial<ContactFormValues> | null;
  // Re-validate server-side with the same rules the form used. The browser's
  // pass is a convenience; this one is the authority.
  const values: ContactFormValues = {
    ...EMPTY_CONTACT_FORM,
    name: typeof raw?.name === "string" ? raw.name : "",
    email: typeof raw?.email === "string" ? raw.email : "",
    company: typeof raw?.company === "string" ? raw.company : "",
    teamSize: typeof raw?.teamSize === "string" ? raw.teamSize : "",
    message: typeof raw?.message === "string" ? raw.message : "",
  };

  const errors = validateContactForm(values);
  if (hasErrors(errors)) {
    return NextResponse.json({ errors }, { status: 422 });
  }

  const resendKey = process.env.RESEND_API_KEY;
  const from = process.env.CONTACT_FROM_EMAIL;
  const to = process.env.CONTACT_TO_EMAIL;
  if (resendKey && from && to) {
    try {
      const email = buildContactEmail(values);
      const response = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${resendKey}` },
        body: JSON.stringify({ from, to: [to], reply_to: email.replyTo, subject: email.subject, text: email.text }),
        signal: AbortSignal.timeout(8_000),
      });
      if (!response.ok) {
        // Resend's body names the problem (unverified domain, bad key) and holds
        // nothing of the visitor's — safe to log, never to return.
        console.error("[contact] resend responded %s: %s", response.status, await response.text().catch(() => ""));
        return NextResponse.json({ error: "The message could not be delivered." }, { status: 502 });
      }
    } catch (error) {
      console.error("[contact] failed to reach resend", error);
      return NextResponse.json({ error: "Could not reach the mail service." }, { status: 502 });
    }
    return NextResponse.json({ ok: true }, { status: 202 });
  }

  const destination = process.env.CONTACT_WEBHOOK_URL;
  if (!destination) {
    console.warn(
      "[contact] no destination is configured (RESEND_API_KEY + CONTACT_FROM_EMAIL + CONTACT_TO_EMAIL, or CONTACT_WEBHOOK_URL) — the submission was rejected rather than dropped.",
    );
    return NextResponse.json(
      { error: "The contact form is not connected to a destination yet." },
      { status: 503 },
    );
  }

  try {
    const response = await fetch(destination, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        source: "orkest-marketing-contact",
        submittedAt: new Date().toISOString(),
        ...values,
      }),
      // Without a timeout a hung destination holds the request open until the
      // platform's own limit, which surfaces to the visitor as a dead button.
      signal: AbortSignal.timeout(8_000),
    });

    if (!response.ok) {
      console.error("[contact] destination responded %s", response.status);
      return NextResponse.json({ error: "The destination rejected the message." }, { status: 502 });
    }
  } catch (error) {
    console.error("[contact] failed to reach destination", error);
    return NextResponse.json({ error: "Could not reach the destination." }, { status: 502 });
  }

  return NextResponse.json({ ok: true }, { status: 202 });
}
