"use client";

import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/States";
import styles from "./jobs.module.css";

/**
 * The next page of a search, by the API's cursor (FRONTEND_UI 3.23). The rows already loaded stay on screen whatever
 * happens here: a failed page shows what failed, in the words for its error code, and Retry asks for that page again.
 */
export function LoadMore({ isPending, error, onLoad }: { isPending: boolean; error: Error | null; onLoad: () => void }) {
  if (error !== null) return <ErrorState what="Could not load more jobs." error={error} onRetry={onLoad} />;
  return (
    <div className={styles.more}>
      <Button isPending={isPending} onPress={onLoad}>
        Load more
      </Button>
    </div>
  );
}
