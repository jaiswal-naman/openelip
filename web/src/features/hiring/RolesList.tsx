"use client";

import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import ledger from "@/components/ui/ledger.module.css";
import { ROLE_STATUS_LABELS } from "@/lib/labels";
import { type RoleListItem, useMembership, useRoles } from "./api";
import { CreateOrganization } from "./CreateOrganization";
import { Invitations } from "./Invitations";
import styles from "./hiring.module.css";

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const owed = (role: RoleListItem) =>
  role.answers_owed === 0 ? "-" : role.answers_overdue > 0 ? `${role.answers_owed} (${role.answers_overdue} overdue)` : String(role.answers_owed);
const target = (role: RoleListItem) => (role.status === "draft" ? `/hire/roles/${role.id}` : `/hire/shortlist/${role.id}`);

/** The cells come from the list itself: a count of roles, of people who said yes, and of answers owed. Nothing is guessed. */
function titleCells(roles: RoleListItem[]): TitleCell[] {
  const owedTotal = roles.reduce((sum, role) => sum + role.answers_owed, 0);
  const overdue = roles.reduce((sum, role) => sum + role.answers_overdue, 0);
  return [
    { label: "Open roles", value: roles.filter((role) => role.status === "open").length },
    { label: "Drafts", value: roles.filter((role) => role.status === "draft").length, tone: "muted" },
    { label: "Said yes", value: roles.reduce((sum, role) => sum + role.said_yes, 0) },
    {
      label: "Answers owed",
      tone: owedTotal > 0 ? "redline" : "muted",
      value: (
        <>
          {owedTotal}
          {overdue > 0 && <span className={styles.sub}>{overdue} overdue</span>}
        </>
      ),
    },
  ];
}

/** /hire/roles: the organization's roles and what is owed, or, with no organization, invitations and the create form. */
export function RolesList() {
  const { membership, invitations, isLoading } = useMembership();
  if (isLoading) return <p role="status" className="muted">Loading.</p>;
  if (membership === null) {
    return (
      <div className={styles.page}>
        <TitleBlock title="Roles" lines={<p>Create your organization, or join one you were invited to, to post roles.</p>} />
        <Invitations invitations={invitations} />
        <CreateOrganization />
      </div>
    );
  }
  return <Roles orgId={membership.id} orgName={membership.name} />;
}

function Roles({ orgId, orgName }: { orgId: string; orgName: string }) {
  const roles = useRoles(orgId);
  return (
    <div className={styles.page}>
      <TitleBlock
        title="Roles"
        lines={
          <>
            <p className={styles.company}>{orgName}</p>
            <p className="t-small">The roles you posted, who said yes to each, and the answers you owe.</p>
          </>
        }
        cells={roles.data === undefined ? [] : titleCells(roles.data)}
        actions={
          <LinkButton href="/hire/roles/new" variant="primary">
            New role
          </LinkButton>
        }
      />
      {roles.isError ? (
        <ErrorState what="Could not load your roles." error={roles.error} onRetry={() => void roles.refetch()} />
      ) : roles.data === undefined ? (
        <p role="status" className="muted">Loading your roles.</p>
      ) : roles.data.length === 0 ? (
        <EmptyState>No roles yet. Describe one to see who would be asked.</EmptyState>
      ) : (
        <Table aria-label="Roles" className={`${ledger.table} ${styles.tight}`}>
          <TableHeader>
            <Column isRowHeader>Role</Column>
            <Column>Status</Column>
            <Column className={ledger.num}>Said yes</Column>
            <Column className={ledger.num}>Answers owed</Column>
            <Column>Updated</Column>
          </TableHeader>
          <TableBody>
            {roles.data.map((role) => (
              <Row key={role.id} id={role.id} textValue={role.title} href={target(role)} className={styles.rowLink}>
                <Cell className={styles.words}>{role.title}</Cell>
                <Cell data-label="Status">
                  <Pill tone={role.status === "open" ? "ok" : "stage"}>{ROLE_STATUS_LABELS[role.status]}</Pill>
                </Cell>
                <Cell className={ledger.num} data-label="Said yes">
                  {role.said_yes === 0 ? "-" : role.said_yes}
                </Cell>
                <Cell className={`${ledger.num} ${styles.owed}`} data-label="Answers owed" data-overdue={role.answers_overdue > 0 || undefined}>
                  {owed(role)}
                </Cell>
                <Cell data-label="Updated">
                  <span className="t-data">{day(role.updated_at)}</span>
                </Cell>
              </Row>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
