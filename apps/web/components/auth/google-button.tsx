"use client";

import * as React from "react";

import { buttonVariants } from "@/components/ui/button";
import { authApi } from "@/lib/api";
import { apiBaseUrl } from "@/lib/api-client";
import { cn } from "@/lib/utils";

/** Google's "G" in its brand colours. Google's guidelines require the multicolour mark on a neutral button. */
function GoogleMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" aria-hidden className={className}>
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}

// Asked once per page load and shared: login and register both render the button,
// and a visitor moving between them should not wait on the same answer twice.
let providersPromise: Promise<boolean> | null = null;
function googleEnabled(): Promise<boolean> {
  providersPromise ??= authApi
    .providers()
    .then((p) => p.google)
    // Unreachable API or an old deployment without the route: no button. The
    // email form is always there, so failing closed costs nothing.
    .catch(() => false);
  return providersPromise;
}

/**
 * "Continue with Google", plus the divider beneath it.
 *
 * Renders only when the deployment has Google configured (`GET /auth/providers`),
 * so a fresh clone with no client id shows the plain email form and no dead
 * button. While the answer is pending it reserves the button's height, so the
 * form does not jump when it arrives.
 *
 * It is a plain `<a>`, not a router link: the next step is a full-page navigation
 * to the API, which 302s to Google. A client-side route change would never leave
 * the SPA.
 */
export function GoogleSignIn({ label = "Continue with Google" }: { label?: string }) {
  const [enabled, setEnabled] = React.useState<boolean | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    googleEnabled().then((value) => {
      if (!cancelled) setEnabled(value);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (enabled === false) return null;
  if (enabled === null) return <div className="mb-5 h-10" aria-hidden />;

  return (
    <div className="mb-5">
      <a
        href={`${apiBaseUrl}/auth/google/login`}
        className={cn(buttonVariants({ variant: "outline", size: "lg" }), "w-full gap-2.5 font-medium")}
      >
        <GoogleMark className="size-[18px]" />
        {label}
      </a>
      <div className="mt-5 flex items-center gap-3 text-xs text-muted-foreground" role="separator" aria-label="or">
        <span className="h-px flex-1 bg-border" />
        or with email
        <span className="h-px flex-1 bg-border" />
      </div>
    </div>
  );
}
