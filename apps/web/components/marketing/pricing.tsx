"use client";

import * as React from "react";
import Link from "next/link";
import { Check, Star } from "lucide-react";

import { gsap } from "@/lib/gsap";
import { useGsapReveal } from "@/hooks/use-gsap-reveal";
import { cn } from "@/lib/utils";

/**
 * Pricing block: a free tier and an enterprise conversation.
 *
 * Until 2026-10-02 this showed three priced plans with a monthly/annual toggle.
 * None of it was real — there is no billing integration, no plan enforcement and
 * no Stripe — so a prospect who read "Team, $199" was reading a claim the
 * product could not honour. Every line below is therefore something the platform
 * actually does today, and nothing here may promise a feature that is not built
 * (SSO and SCIM were on the old Enterprise card and do not exist).
 *
 * The one number, "1,000 runs a day", is `DAILY_RUN_QUOTA_PER_ORG`'s production
 * default in `infra/docker-compose.prod.yml`. Change them together.
 *
 * GSAP drives the entrance, as elsewhere on the landing page; the old toggle's
 * NumberFlow and confetti dependencies went with the toggle.
 */
export interface PricingPlan {
  name: string;
  /** The headline figure. A literal, not a number: "Free" and "Custom" are not amounts. */
  headline: string;
  /** Shown beside the headline, e.g. "forever". Empty for none. */
  qualifier: string;
  features: string[];
  description: string;
  buttonText: string;
  href: string;
  isPopular: boolean;
}

export const ORKEST_PLANS: PricingPlan[] = [
  {
    name: "Free",
    headline: "$0",
    qualifier: "to start",
    features: [
      "Unlimited workflows and workspaces",
      "Up to 1,000 runs per day",
      "Human approval gate on every mutating step",
      "Immutable audit trail",
      "Webhook, schedule and manual triggers",
      "Knowledge base with cited retrieval",
      "Bring your own OpenAI key — model usage bills to you at cost",
    ],
    description: "Everything the product does. The approval gate and the audit trail are not an upgrade.",
    buttonText: "Start free",
    href: "/register",
    isPopular: true,
  },
  {
    name: "Enterprise",
    headline: "Let's talk",
    qualifier: "",
    features: [
      "Higher run limits",
      "Self-hosted or private-cloud deployment",
      "Integrations built against your ERP, HR and finance systems",
      "A walkthrough of your own workflows, end to end",
      "Direct line to the engineers who built it",
    ],
    description: "For teams that need it running inside their own environment, or wired to systems we have not met yet.",
    buttonText: "Talk to us",
    href: "#contact",
    isPopular: false,
  },
];

export function Pricing({
  plans = ORKEST_PLANS,
  title = "Free to start. Talk to us when it matters.",
  description = "No seats, no credit card. You bring your own model key, so what the models cost is between you and OpenAI — we do not mark it up.",
}: {
  plans?: PricingPlan[];
  title?: string;
  description?: string;
}) {
  const rootRef = React.useRef<HTMLElement>(null);

  useGsapReveal(rootRef, () => {
    gsap.from("[data-plan]", {
      y: 46,
      opacity: 0,
      duration: 0.8,
      ease: "power3.out",
      stagger: 0.1,
      scrollTrigger: { trigger: rootRef.current, start: "top 74%", once: true },
      onComplete: () => gsap.set("[data-plan]", { clearProps: "all" }),
    });
  });

  return (
    <section ref={rootRef} id="pricing" className="bg-mk-paper px-5 pb-24 sm:pb-32">
      <div className="mx-auto max-w-4xl">
        <div className="mx-auto max-w-2xl text-center">
          <p className="mk-eyebrow text-mk-sky-deep">Pricing</p>
          <h2 className="mk-display mt-3 text-[2rem] text-mk-ink sm:text-[2.75rem]">{title}</h2>
          <p className="mt-4 text-[1.0625rem] leading-relaxed text-mk-ink-soft">{description}</p>
        </div>

        <div className="mt-14 grid grid-cols-1 items-stretch gap-5 md:grid-cols-2">
          {plans.map((plan) => (
            <article
              key={plan.name}
              data-plan
              className={cn(
                "relative flex flex-col rounded-3xl border bg-white p-7",
                plan.isPopular
                  ? "border-mk-ink shadow-[0_1px_2px_rgba(20,20,20,0.05),0_16px_40px_-12px_rgba(20,20,20,0.16)]"
                  : "border-[var(--mk-hairline)] mk-lift",
              )}
            >
              {plan.isPopular ? (
                <span className="absolute -top-3 left-7 flex items-center gap-1 rounded-full bg-mk-ink px-3 py-1 text-[11px] font-semibold whitespace-nowrap text-white">
                  <Star className="size-3 fill-mk-lime text-mk-lime" aria-hidden />
                  Start here
                </span>
              ) : null}

              <p className="text-[0.8125rem] font-semibold tracking-wide text-mk-ink-soft uppercase">
                {plan.name}
              </p>

              <div className="mt-5 flex items-baseline gap-2">
                <span className="mk-display text-[3rem] leading-none text-mk-ink">{plan.headline}</span>
                {plan.qualifier ? (
                  <span className="text-[0.875rem] font-medium text-mk-ink-soft">{plan.qualifier}</span>
                ) : null}
              </div>

              <ul className="mt-7 flex flex-col gap-2.5">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-start gap-2.5">
                    <Check className="mt-[3px] size-3.5 shrink-0 text-mk-sky-deep" aria-hidden />
                    <span className="text-[0.875rem] leading-snug text-mk-ink">{feature}</span>
                  </li>
                ))}
              </ul>

              <div className="mt-8 flex flex-1 flex-col justify-end">
                <Link
                  href={plan.href}
                  className={cn(
                    "flex w-full items-center justify-center rounded-full px-5 py-3 text-[0.9375rem] font-semibold tracking-tight transition-colors outline-none focus-visible:ring-3 focus-visible:ring-mk-sky/40",
                    plan.isPopular
                      ? "bg-mk-lime text-mk-ink hover:bg-mk-lime-deep"
                      : "border border-mk-ink/15 bg-white text-mk-ink hover:bg-mk-mist",
                  )}
                >
                  {plan.buttonText}
                </Link>
                <p className="mt-4 text-[0.75rem] leading-relaxed text-mk-ink-soft">
                  {plan.description}
                </p>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
