"use client";

import { useState } from "react";
import { Cell, Column, Form, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { TextField } from "@/components/ui/TextField";
import { NativeSelect } from "@/ui/patterns/NativeSelect";
import { describeFailure } from "@/lib/api/problems";
import { MEMBER_STATUS_LABELS, ORG_ROLE_LABELS } from "@/lib/labels";
import { type Member, type OrgRole, useChangeMemberRole, useClaimOwnership, useInvite, useMembership, useOrg, useRemoveMember } from "./api";
import { ConfirmDialog } from "./ConfirmDialog";
import styles from "./hiring.module.css";

/** /hire/team: members and pending invitations. Hidden buttons are for clarity; the server enforces who may do what (T-06-37). */
export function Team() {
  const { membership, isLoading } = useMembership();
  if (isLoading) return <p role="status" className="muted">Loading.</p>;
  if (membership === null) return <EmptyState>Create your organization on the Roles page first.</EmptyState>;
  return <TeamOf orgId={membership.id} />;
}

function TeamOf({ orgId }: { orgId: string }) {
  const org = useOrg(orgId);
  const change = useChangeMemberRole(orgId);
  const remove = useRemoveMember(orgId);
  const claim = useClaimOwnership(orgId);
  if (org.isError) return <ErrorState what="Could not load your team." error={org.error} onRetry={() => void org.refetch()} />;
  if (org.data === undefined) return <p role="status" className="muted">Loading your team.</p>;

  const { members, role: mine } = org.data;
  const canManage = mine === "owner" || mine === "admin";
  const owners = members.filter((member) => member.role === "owner" && member.status === "active").length;
  const failure = change.error ?? claim.error;

  return (
    <div className={styles.page}>
      <h1 className="t-page">Team, {org.data.name}</h1>
      {owners === 0 && mine === "admin" && (
        <div className={styles.card}>
          <p>This organization has no owner.</p>
          <div className={styles.row}>
            <Button variant="primary" isPending={claim.isPending} onPress={() => claim.mutate()}>
              Claim ownership
            </Button>
          </div>
        </div>
      )}
      {failure && (
        <p role="alert" className={styles.problem}>
          {describeFailure(failure)}
        </p>
      )}
      <div className={styles.tableWrap}>
        <Table aria-label="Members" className={styles.table}>
          <TableHeader>
            {["Name", "Email", "Role", "Status", "Actions"].map((name, index) => (
              <Column key={name} isRowHeader={index === 0} className={`t-label ${styles.column}`}>
                {name}
              </Column>
            ))}
          </TableHeader>
          <TableBody>
            {members.map((member) => (
              <Row key={member.member_id} id={member.member_id} textValue={member.display_name ?? member.email ?? "Member"}>
                <Cell className={styles.cell}>{member.display_name ?? "-"}</Cell>
                <Cell className={styles.cell}>{member.email ?? "-"}</Cell>
                <Cell className={styles.cell}>
                  {canManage && member.role !== "owner" ? (
                    <NativeSelect
                      label={`Role of ${member.email ?? member.display_name ?? "member"}`}
                      value={member.role}
                      options={(["member", "admin"] as const).map((role) => ({ value: role, label: ORG_ROLE_LABELS[role] }))}
                      onChange={(role) => change.mutate({ memberId: member.member_id, role: role as OrgRole })}
                    />
                  ) : (
                    ORG_ROLE_LABELS[member.role]
                  )}
                </Cell>
                <Cell className={styles.cell}>{MEMBER_STATUS_LABELS[member.status]}</Cell>
                <Cell className={styles.cell}>
                  {canManage && !(member.role === "owner" && owners <= 1) && (
                    <RemoveButton member={member} onRemove={() => remove.mutateAsync(member.member_id)} />
                  )}
                </Cell>
              </Row>
            ))}
          </TableBody>
        </Table>
      </div>
      {canManage ? <InviteForm orgId={orgId} /> : <p className={`t-small ${styles.muted}`}>Owners and admins invite people and change roles.</p>}
    </div>
  );
}

function RemoveButton({ member, onRemove }: { member: Member; onRemove: () => Promise<unknown> }) {
  const pending = member.status === "invited";
  const who = member.email ?? member.display_name ?? "this member";
  return (
    <ConfirmDialog
      trigger={pending ? `Revoke invitation for ${who}` : `Remove ${who}`}
      title={pending ? "Revoke this invitation?" : "Remove this member?"}
      confirm={pending ? "Revoke" : "Remove"}
      onConfirm={onRemove}
    >
      {pending ? `${who} will no longer be able to join.` : `${who} loses access to this organization's roles and answers.`}
    </ConfirmDialog>
  );
}

function InviteForm({ orgId }: { orgId: string }) {
  const invite = useInvite(orgId);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "admin">("member");
  return (
    <section aria-labelledby="invite" className={styles.card}>
      <h2 id="invite" className="t-section">
        Invite by email
      </h2>
      <Form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          invite.mutate({ email: email.trim(), role }, { onSuccess: () => setEmail("") });
        }}
      >
        <TextField label="Email address" type="email" value={email} onChange={setEmail} isRequired />
        <NativeSelect
          label="Role"
          value={role}
          options={(["member", "admin"] as const).map((value) => ({ value, label: ORG_ROLE_LABELS[value] }))}
          onChange={(value) => setRole(value as "member" | "admin")}
        />
        {invite.isError && (
          <p role="alert" className={styles.problem}>
            {describeFailure(invite.error)}
          </p>
        )}
        {invite.isSuccess && <p role="status">Invitation sent.</p>}
        <div className={styles.row}>
          <Button type="submit" variant="primary" isPending={invite.isPending}>
            Send invitation
          </Button>
        </div>
      </Form>
    </section>
  );
}
