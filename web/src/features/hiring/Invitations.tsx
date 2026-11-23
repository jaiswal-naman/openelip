"use client";

import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { describeFailure } from "@/lib/api/problems";
import { ORG_ROLE_LABELS } from "@/lib/labels";
import { type Invitation, useJoinOrganization } from "./api";
import styles from "./hiring.module.css";

/** Pending invitations to the person's address, each with Join (INDEX deviation 19). */
export function Invitations({ invitations }: { invitations: Invitation[] }) {
  const router = useRouter();
  const join = useJoinOrganization();
  if (invitations.length === 0) return null;
  return (
    <section aria-labelledby="invitations" className={styles.stack}>
      <h2 id="invitations" className="t-section">
        You were invited
      </h2>
      <ul className={styles.invited}>
        {invitations.map((invitation) => (
          <li key={invitation.organization_id}>
            <span className={styles.words}>
              {invitation.organization_name} ({ORG_ROLE_LABELS[invitation.role]})
            </span>
            <Button
              variant="primary"
              size="sm"
              aria-label={`Join ${invitation.organization_name}`}
              isPending={join.isPending && join.variables === invitation.organization_id}
              onPress={() => join.mutate(invitation.organization_id, { onSuccess: () => router.refresh() })}
            >
              Join
            </Button>
          </li>
        ))}
      </ul>
      {join.isError && (
        <p role="alert" className={styles.problem}>
          {describeFailure(join.error)} You did not join.
        </p>
      )}
    </section>
  );
}
