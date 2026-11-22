"use client";

import { buttonClass } from "@/components/ui/buttonStyles";
import "@/styles/tokens.css";
import "@/styles/global.css";

/** Replaces the root layout when it fails, so it brings its own <html> and <body>. It follows the system theme. */
export default function GlobalError({ retry }: { error: unknown; retry: () => void }) {
  return (
    <html lang="en">
      <body>
        <main className="container page">
          <h1 className="t-page">Something went wrong</h1>
          <p>
            The page could not be shown. Nothing was changed. Try again, and if it repeats, tell the person who runs this
            server.
          </p>
          <p>
            <button type="button" className={buttonClass("primary")} onClick={() => retry()}>
              Retry
            </button>
          </p>
        </main>
      </body>
    </html>
  );
}
