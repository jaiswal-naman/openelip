"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { Pill } from "@/components/ui/Pill";
import { useMembership } from "./api";
import { PauseBadge } from "./PauseBadge";
import styles from "./hiring.module.css";

// A shortlist belongs to a role, so it keeps the Roles tab current.
const LINKS = [
  { href: "/hire/roles", label: "Roles", under: ["/hire/roles", "/hire/shortlist"] },
  { href: "/hire/answers", label: "Answers", under: ["/hire/answers"] },
  { href: "/hire/team", label: "Team", under: ["/hire/team"] },
] as const;

/** The hiring desk's frame: the sections as tabs, the organization's plate (with the demo chip) and the org-wide pause badge. */
export function HiringShell({ children }: { children: ReactNode }) {
  const { membership } = useMembership();
  const pathname = usePathname();
  return (
    <div className={styles.frame}>
      {membership && (
        <>
          <div className={styles.bar}>
            <span className={styles.orgName}>
              <span className={styles.words}>{membership.name}</span>
              {membership.is_synthetic && <Pill tone="unk">Demo (invented)</Pill>}
            </span>
            <nav aria-label="Hiring desk" className={styles.nav}>
              {LINKS.map((link) => (
                <Link key={link.href} href={link.href} aria-current={link.under.some((prefix) => pathname.startsWith(prefix)) ? "page" : undefined}>
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
