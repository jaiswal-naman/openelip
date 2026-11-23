"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import styles from "./header.module.css";

/** A register entry that says which sheet is open, to the eye (an ink cell) and to screen readers (aria-current). */
export function NavLink({ href, icon, children }: { href: string; icon?: ReactNode; children: ReactNode }) {
  const pathname = usePathname();
  const isCurrent = pathname === href || pathname.startsWith(`${href}/`);
  return (
    <li>
      <Link href={href} className={styles.link} aria-current={isCurrent ? "page" : undefined}>
        {icon}
        <span>{children}</span>
      </Link>
    </li>
  );
}
