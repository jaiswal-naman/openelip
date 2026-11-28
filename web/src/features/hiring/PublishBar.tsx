"use client";

import { Button } from "@/components/ui/Button";
import { describeFailure } from "@/lib/api/problems";
import { PUBLISH_BLOCKER_LABELS } from "@/lib/labels";
import { type RoleView, usePublishRole } from "./api";
import styles from "./hiring.module.css";

const ITEMS = ["requirements_unconfirmed", "pay_range_required"] as const;

/**
 * Publish, with the reason it is not possible printed beside it (HIR-02), never in a tooltip. The marks come from the role's
 * own `blockers`; a 422 or 409 from the server is said in the same words. Publishing is the one outside action of this page,
 * so what it does is said under the button.
 */
export function PublishBar({ role }: { role: RoleView }) {
  const publish = usePublishRole(role.id, role.organization_id);
  const blockers = new Set<string>(role.blockers);
  return (
    <section aria-label="Publish" className={styles.publish}>
      <div className={styles.publishRow}>
        <Button variant="primary" aria-describedby="publish-needs" isDisabled={!role.can_publish} isPending={publish.isPending} onPress={() => publish.mutate()}>
          Publish
        </Button>
        <p id="publish-needs" className={styles.blockers}>
          <span>Publishing needs:</span>
          {ITEMS.map((code) => (
            <span key={code} data-state={blockers.has(code) ? "missing" : "done"}>
              {PUBLISH_BLOCKER_LABELS[code]} ({blockers.has(code) ? "missing" : "done"})
            </span>
          ))}
        </p>
      </div>
      <p className={`t-small ${styles.consequence}`}>
        Publishing asks the people whose records fit, privately. Each has 7 days to reply. You see a name only if they accept, and their email only after you answer yes.
      </p>
      {publish.isError && (
        <p role="alert" className={styles.problem}>
          {describeFailure(publish.error)}
        </p>
      )}
    </section>
  );
}
