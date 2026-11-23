"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { Pill } from "@/components/ui/Pill";
import { useMembership } from "./api";
import { PauseBadge } from "./PauseBadge";
import styles from "./hiring.module.css";

const LINKS = [
  { href: "/hire/roles", label: "Roles" },
  { href: "/hire/answers", label: "Answers" },
  { href: "/hire/team", label: "Team" },
] as const;

/** The hiring desk's frame: the organization (with the demo pill), the three sections and the org-wide pause badge. */
export function HiringShell({ children }: { children: ReactNode }) {
  const { membership } = useMembership();
  const pathname = usePathname();
  return (
    <div className={styles.shell}>
      {membership && (
        <>
          <div className={styles.bar}>
            <span className={styles.orgName}>
              <span className={styles.words}>{membership.name}</span>
              {membership.is_synthetic && <Pill tone="unk">Demo (invented)</Pill>}
            </span>
            <nav aria-label="Hiring desk" className={styles.nav}>
              {LINKS.map((link) => (
                <Link key={link.href} href={link.href} aria-current={pathname.startsWith(link.href) ? "page" : undefined}>
                  {link.label}
                </Link>
              ))}
            </nav>
          </div>
          <PauseBadge orgId={membership.id} />
        </>
      )}
      {children}
    </div>
  );
}
