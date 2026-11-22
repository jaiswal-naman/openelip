"use client";

import { ErrorState } from "@/components/ui/States";

/** Catches a render error in any page below the layout, so the header and navigation keep working. */
export default function PageError({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div className="container page">
      <ErrorState what="This page could not be shown." error={error} onRetry={retry} />
    </div>
  );
}
