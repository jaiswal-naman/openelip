import Link from "next/link";
import { AccountMenu } from "@/features/auth/AccountMenu";
import { PRODUCT_NAME } from "@/lib/product";
import { BrandMark } from "../icons/icons";
import styles from "./header.module.css";
import { NavLink } from "./NavLink";
import { ThemeToggle } from "./ThemeToggle";

export function SiteHeader() {
  return (
    <>
      <a href="#main" className={styles.skip}>
        Skip to main content
      </a>
      <header className={styles.header}>
        <div className={`container ${styles.inner}`}>
          <Link href="/" className={styles.brand} aria-label={`${PRODUCT_NAME} home`}>
            <BrandMark />
            <span className={styles.wordmark}>{PRODUCT_NAME}</span>
          </Link>
          <nav aria-label="Main" className={styles.nav}>
            <NavLink href="/jobs">Jobs</NavLink>
            <NavLink href="/trust">Trust</NavLink>
          </nav>
          <div className={styles.tools}>
            <ThemeToggle />
            <AccountMenu />
          </div>
        </div>
      </header>
    </>
  );
}
