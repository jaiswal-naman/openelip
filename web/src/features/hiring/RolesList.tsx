"use client";

import Link from "next/link";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { Pill } from "@/components/ui/Pill";
import { EmptyState, ErrorState } from "@/components/ui/States";
import { LinkButton } from "@/components/ui/LinkButton";
import { ROLE_STATUS_LABELS } from "@/lib/labels";
import { type RoleListItem, useMembership, useRoles } from "./api";
import { CreateOrganization } from "./CreateOrganization";
import { Invitations } from "./Invitations";
import styles from "./hiring.module.css";

const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const owed = (role: RoleListItem) =>
  role.answers_owed === 0 ? "-" : role.answers_overdue > 0 ? `${role.answers_owed} (${role.answers_overdue} overdue)` : String(role.answers_owed);
const target = (role: RoleListItem) => (role.status === "draft" ? `/hire/roles/${role.id}` : `/hire/shortlist/${role.id}`);

/** /hire/roles: the organization's roles and what is owed, or, with no organization, invitations and the create form. */
export function RolesList() {
  const { membership, invitations, isLoading } = useMembership();
  if (isLoading) return <p role="status" className="muted">Loading.</p>;
  if (membership === null) {
    return (
      <div className={styles.page}>
        <h1 className="t-page">Roles</h1>
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
      <div className={styles.head}>
        <h1 className="t-page">Roles, {orgName}</h1>
        <LinkButton href="/hire/roles/new" variant="primary">
          New role
        </LinkButton>
      </div>
      {roles.isError ? (
        <ErrorState what="Could not load your roles." error={roles.error} onRetry={() => void roles.refetch()} />
      ) : roles.data === undefined ? (
        <p role="status" className="muted">Loading your roles.</p>
      ) : roles.data.length === 0 ? (
        <EmptyState>No roles yet. Describe one to see who would be asked.</EmptyState>
      ) : (
        <>
          <div className={`${styles.tableWrap} ${styles.swap}`}>
            <Table aria-label="Roles" className={styles.table}>
              <TableHeader>
                {["Role", "Status", "Said yes", "Answers owed", "Updated"].map((name, index) => (
                  <Column key={name} isRowHeader={index === 0} className={`t-label ${styles.column}`}>
                    {name}
                  </Column>
                ))}
              </TableHeader>
              <TableBody>
                {roles.data.map((role) => (
                  <Row key={role.id} id={role.id} textValue={role.title} href={target(role)}>
                    <Cell className={styles.cell}>{role.title}</Cell>
                    <Cell className={styles.cell}>{ROLE_STATUS_LABELS[role.status]}</Cell>
                    <Cell className={styles.cell}>{role.said_yes === 0 ? "-" : role.said_yes}</Cell>
                    <Cell className={styles.cell}>{owed(role)}</Cell>
                    <Cell className={styles.cell}>{day(role.updated_at)}</Cell>
                  </Row>
                ))}
              </TableBody>
            </Table>
          </div>
          <ul className={styles.cards} aria-label="Roles">
            {roles.data.map((role) => (
              <li key={role.id} className={styles.card}>
                <Link href={target(role)} className={styles.words}>
                  {role.title}
                </Link>
                <span className={styles.row}>
                  <Pill tone={role.status === "open" ? "ok" : "unk"}>{ROLE_STATUS_LABELS[role.status]}</Pill>
                  <span>Answers owed: {owed(role)}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
