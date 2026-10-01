"use client";

import * as React from "react";
import { OTPInput, OTPInputContext } from "input-otp";

import { cn } from "@/lib/utils";

/**
 * One-time-code input. Built on `input-otp`, which renders a single real <input>
 * (so paste, autofill and `autocomplete="one-time-code"` work natively — the six
 * boxes are only a drawing of its value) and exposes each slot's state through
 * context.
 *
 * Styled on the app's own tokens rather than the shadcn defaults it was supplied
 * with: each slot is a separate rounded box (the segmented-pill default reads as a
 * form from 2019), and the caret is `animate-pulse` because the stock
 * `animate-caret-blink` utility does not exist in this Tailwind setup.
 */
function InputOTP({
  className,
  containerClassName,
  ...props
}: React.ComponentProps<typeof OTPInput>) {
  return (
    <OTPInput
      containerClassName={cn("flex items-center gap-2 has-disabled:opacity-50", containerClassName)}
      className={cn("disabled:cursor-not-allowed", className)}
      {...props}
    />
  );
}

function InputOTPGroup({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex items-center gap-2", className)} {...props} />;
}

function InputOTPSlot({
  index,
  className,
  ...props
}: React.ComponentProps<"div"> & { index: number }) {
  const context = React.useContext(OTPInputContext);
  const { char, hasFakeCaret, isActive } = context.slots[index];

  return (
    <div
      data-slot="input-otp-slot"
      data-active={isActive}
      className={cn(
        "relative flex h-12 w-10 items-center justify-center rounded-xl border border-input bg-background text-lg font-semibold tabular-nums transition-all sm:w-11",
        isActive && "z-10 border-ring ring-3 ring-ring/25",
        className,
      )}
      {...props}
    >
      {char}
      {hasFakeCaret ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="h-5 w-px animate-pulse bg-foreground" />
        </div>
      ) : null}
    </div>
  );
}

function InputOTPSeparator({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div role="separator" className={cn("h-px w-2.5 shrink-0 bg-border", className)} {...props} />
  );
}

export { InputOTP, InputOTPGroup, InputOTPSlot, InputOTPSeparator };
