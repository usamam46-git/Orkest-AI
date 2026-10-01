"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useLenis } from "lenis/react";

import ScrambleText from "@/components/ui/scramble-text";
import { InteractiveHoverButton } from "@/components/ui/interactive-hover-button";
import { usePrefersReducedMotion } from "@/hooks/use-media-query";
import { cn } from "@/lib/utils";

/**
 * The hero copy, over the office.
 *
 * ## Redesigned 2026-10-02: left-aligned, sequenced, and shorter
 *
 * It used to be a centred stack — eyebrow, two-line headline, a long paragraph, a
 * row of three icon bullets, two pills — every element centred on the room. That
 * is the shape of a thousand landing pages, and the page read as templated before
 * a word of it was read. Three changes, none of them to the photograph:
 *
 * - **Left-aligned, on the content column.** The plate's right third is a busy
 *   bookcase and its left two thirds is a calm wall with a bright window, so the
 *   block sits where the photograph is quiet and the bookcase is left alone. It
 *   aligns to the same `max-w-6xl` column as every section below it.
 * - **The icon row is gone.** "Row-level tenant isolation / append-only audit
 *   trail / bring your own key" is a feature trio, and the platform tiles further
 *   down already make each of those claims with evidence. Repeating them here as
 *   three icons was the single most template-shaped thing on the screen.
 * - **The headline resolves instead of appearing.** Each line scrambles into
 *   place (`ScrambleText`), staggered, and the sentence and buttons rise in behind
 *   it. Slow enough to be seen, but everything is on screen by ~2.1s: a call to
 *   action that waits five seconds for an animation is a conversion cost.
 *
 * ## What must not break
 *
 * - **The headline stays in the server-rendered HTML.** `ScrambleText` renders the
 *   full text in an `sr-only` span, so the H1's text is in the initial markup and
 *   a screen reader reads it once, whole. Each line also keeps an invisible copy
 *   of itself as a spacer, so the block's height is final from the first paint and
 *   nothing below it moves while letters resolve.
 * - **It must never strand a blank hero.** `use-scramble` runs on
 *   `requestAnimationFrame`, which does not tick in a background tab — the same
 *   hazard that made GSAP `from(opacity: 0)` blank this page once. If the first
 *   line has not finished within `FALLBACK_MS`, every line is shown as plain text.
 *   Reduced-motion visitors get plain text and no sequence at all.
 * - **No GSAP here.** The reveals are CSS transitions driven by a step counter, so
 *   they cannot leave an element at opacity 0 the way an unguarded `gsap.from`
 *   does.
 * - **The block stops above the table edge** (`PLATE_DESK_EDGE_NDC`, 72% of
 *   frame). The wood carries the paperwork and must stay clean; the buttons are
 *   the lowest element and are opaque, so documents can come right up under them.
 * - **It scrolls away AND fades on its own** — see `core-scene.tsx`; none of that
 *   is touched here.
 *
 * ## Neither button is lime, and the type is near-opaque ink
 *
 * Both unchanged and for the same reasons. Lime on warm timber reads as neon, so
 * the primary is ink with white text and the secondary is paper with a hairline.
 * Translucent ink has a contrast ceiling no background can lift (45% ink reaches
 * ~3.4:1 even on pure white, and measured 1.0:1 over the plate), so the tones are
 * 80/70/90% and the plate carries a measured 45% wash. **Moving the block off the
 * bookcase changes which pixels sit behind the words, so the figures recorded in
 * `apps/web/CLAUDE.md` describe the old position and must be re-measured** (worst
 * 8px patch per text line, per `Range.getClientRects()`).
 */

/** The headline, one entry per visual line. Each resolves on its own beat. */
const HEADLINE = ["Automation that", "knows when to ask"] as const;

/** Milliseconds from mount. The whole sequence is visible by `cta`. */
const BEATS = { eyebrow: 150, line1: 450, line2: 1000, body: 1800, cta: 2150 } as const;

/** If the first line has not finished by here the tab is not animating; show plain text. */
const FALLBACK_MS = 4500;

/** How fast each line scrambles. Lower is slower; `ScrambleText` divides by 100. */
const SCRAMBLE_SPEED = 55;

/**
 * 0 = nothing, 1 = eyebrow, 2 = line one starts, 3 = line two starts, 4 = sentence,
 * 5 = buttons. Reduced motion jumps straight to the end.
 */
function useSequence(reducedMotion: boolean): number {
  const [step, setStep] = React.useState(0);

  React.useEffect(() => {
    if (reducedMotion) return;
    const beats = [BEATS.eyebrow, BEATS.line1, BEATS.line2, BEATS.body, BEATS.cta];
    const timers = beats.map((ms, index) => window.setTimeout(() => setStep(index + 1), ms));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [reducedMotion]);

  // Derived, not stored: reduced motion needs no sequence at all.
  return reducedMotion ? 5 : step;
}

/** A reveal: fades and rises into place when `shown`. Layout space is reserved either way. */
const reveal = (shown: boolean) =>
  cn(
    "transition-[opacity,transform] duration-[900ms] ease-out motion-reduce:transition-none",
    shown ? "translate-y-0 opacity-100" : "translate-y-3 opacity-0",
  );

function HeadlineLine({
  text,
  active,
  plain,
  className,
  onComplete,
}: {
  text: string;
  active: boolean;
  plain: boolean;
  className?: string;
  onComplete?: () => void;
}) {
  if (plain) {
    return <span className={cn("block", className)}>{text}</span>;
  }

  return (
    <span className={cn("relative block", className)}>
      {/* Reserves the line's final box so the block never reflows as letters resolve. */}
      <span aria-hidden className="invisible block">
        {text}
      </span>
      <span className="absolute inset-0 block">
        {active ? (
          <ScrambleText text={text} speed={SCRAMBLE_SPEED} onComplete={onComplete} />
        ) : (
          // Not started yet: still give assistive tech and the server HTML the words.
          <span className="sr-only">{text}</span>
        )}
      </span>
    </span>
  );
}

export function HeroCopy() {
  const router = useRouter();
  const lenis = useLenis();
  const reducedMotion = usePrefersReducedMotion();
  const step = useSequence(reducedMotion);

  const firstLineDone = React.useRef(false);
  const [plain, setPlain] = React.useState(false);

  React.useEffect(() => {
    if (reducedMotion) return;
    const timer = window.setTimeout(() => {
      if (!firstLineDone.current) setPlain(true);
    }, FALLBACK_MS);
    return () => window.clearTimeout(timer);
  }, [reducedMotion]);

  /**
   * "Watch a run" jumps to the run scene.
   *
   * `#how-it-works` is a zero-height marker inside the scene's 420vh scroll
   * container, positioned by `sceneAnchorTopVh` — it is a scroll POSITION, not
   * a section. Lenis owns the document scroll on this page
   * (`components/marketing/smooth-scroll.tsx`), and a native
   * `scrollIntoView({ behavior: "smooth" })` runs the browser's own animation
   * against it, so the two fight over the same position.
   *
   * `offset: 0` mirrors the explicit `scrollMarginTop: 0` on that marker: the
   * sticky stage fills the viewport, so aligning to the very top is exactly
   * right here. The fallback keeps the button working if the Lenis context is
   * ever absent — nothing on the page should depend on the smoothing existing.
   */
  const scrollToRun = () => {
    if (lenis) {
      lenis.scrollTo("#how-it-works", { offset: 0 });
      return;
    }
    document.getElementById("how-it-works")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="mx-auto w-full max-w-6xl px-5">
      {/* Without JS nothing advances the step counter, so the reveals would stay at
          opacity 0. This keeps them visible. */}
      <noscript>
        <style>{"[data-hero-reveal]{opacity:1!important;transform:none!important}"}</style>
      </noscript>

      <div className="max-w-[40rem] text-left">
        <p
          data-hero-reveal
          className={cn("mk-eyebrow flex items-center gap-3 text-mk-ink/80", reveal(step >= 1))}
        >
          <span aria-hidden className="h-px w-8 bg-mk-ink/60" />
          Workflow automation for ERP, HR and Finance
        </p>

        <h1 className="mk-display mt-5 text-[2.75rem] leading-[1.02] text-mk-ink sm:text-6xl lg:text-[4.5rem]">
          <HeadlineLine
            text={HEADLINE[0]}
            active={step >= 2}
            plain={plain || reducedMotion}
            onComplete={() => {
              firstLineDone.current = true;
            }}
          />
          <HeadlineLine
            text={HEADLINE[1]}
            active={step >= 3}
            plain={plain || reducedMotion}
            className="text-mk-ink/70"
          />
        </h1>

        <p
          data-hero-reveal
          className={cn(
            "mt-6 max-w-[30rem] text-[1.0625rem] leading-relaxed font-medium text-mk-ink/90 sm:text-[1.125rem]",
            reveal(step >= 4),
          )}
        >
          Orkest runs your back-office workflows end to end, then stops for a person before
          anything touches your ledger.
        </p>

        <div
          data-hero-reveal
          className={cn(
            "mt-8 flex flex-wrap items-center gap-3",
            reveal(step >= 5),
            // The wrapper above is pointer-events-none; only enable the buttons once visible.
            step >= 5 ? "pointer-events-auto" : "pointer-events-none",
          )}
        >
          <InteractiveHoverButton
            text="Start building"
            tone="solid"
            onClick={() => router.push("/register")}
          />
          <InteractiveHoverButton text="Watch a run" tone="quiet" onClick={() => scrollToRun()} />
        </div>
      </div>
    </div>
  );
}
