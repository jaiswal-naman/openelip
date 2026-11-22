"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import styles from "./header.module.css";

/** A header link that says which page it is, to the eye (accent underline) and to screen readers (aria-current). */
export function NavLink({ href, children }: { href: string; children: ReactNode }) {
  const pathname = usePathname();
  const isCurrent = pathname === href || pathname.startsWith(`${href}/`);
  return (
    <Link href={href} className={styles.link} aria-current={isCurrent ? "page" : undefined}>
      {children}
    </Link>
  );
}
