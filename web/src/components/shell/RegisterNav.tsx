"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useId, useRef, useState } from "react";
import { AccountMenu } from "@/features/auth/AccountMenu";
import { useMe } from "@/features/auth/queries";
import { ChatOpener } from "@/features/chat/ChatOpener";
import { localStatusQuery } from "@/features/local/queries";
import { useApi } from "@/lib/api/context";
import { PRODUCT_NAME } from "@/lib/product";
import {
  BrandMark,
  BriefcaseIcon,
  CalendarIcon,
  ClipboardListIcon,
  FileTextIcon,
  MenuIcon,
  MessageSquareIcon,
  ReceiptIcon,
  SendIcon,
  ShieldCheckIcon,
  SlidersIcon,
  UsersIcon,
} from "../icons/icons";
import styles from "./header.module.css";
import { NavLink } from "./NavLink";
import { ThemeToggle } from "./ThemeToggle";

function Group({ label, children }: { label?: string; children: ReactNode }) {
  const id = useId();
  return (
    <div className={styles.group}>
      {label && (
        <span id={id} className={`t-label ${styles.groupLabel}`}>
          {label}
        </span>
      )}
      <ul aria-labelledby={label ? id : undefined}>{children}</ul>
    </div>
  );
}

/** Local mode: which model the agents call, and whether it has a key. The key itself never reaches the browser. */
function ModelCell() {
  const api = useApi();
  const status = useQuery(localStatusQuery(api));
  const model = status.data?.model;
  return (
    <Link href="/setup" className={styles.modelCell}>
      <span className="t-label">Model key</span>
      <span className={styles.modelValue} data-missing={model !== undefined && !model.key_set ? true : undefined}>
        {model === undefined ? "…" : model.key_set ? `${model.provider} ···${model.key_last4 ?? ""}` : "Not set. Add one"}
      </span>
    </Link>
  );
}

export function RegisterNav({ local }: { local: boolean }) {
  const me = useMe();
  const pathname = usePathname();
  // The page the folded sidebar was opened on: following a link from it changes the page, which closes it
  const [openOn, setOpenOn] = useState<string | null>(null);
  const open = openOn === pathname;
  const button = useRef<HTMLButtonElement>(null);
  const signedIn = Boolean(me.data);
  const hiring = signedIn && (local || (me.data?.organizations ?? []).length > 0);


  return (
    <>
      <div className={styles.bar}>
        <Link href={local ? "/app/today" : "/"} className={styles.brand} aria-label={`${PRODUCT_NAME} home`}>
          <BrandMark />
          <span className={styles.wordmark}>{PRODUCT_NAME}</span>
        </Link>
        <button
          ref={button}
          type="button"
          className={styles.menuButton}
          aria-expanded={open}
          aria-controls="register-panel"
          onClick={() => setOpenOn(open ? null : pathname)}
        >
          <MenuIcon size={20} />
          <span>Menu</span>
        </button>
      </div>
      <div
        id="register-panel"
        className={styles.panel}
        data-open={open || undefined}
        onKeyDown={(event) => {
          if (event.key === "Escape" && open) {
            setOpenOn(null);
            button.current?.focus();
          }
        }}
      >
        <nav aria-label="Main" className={styles.nav}>
          {signedIn ? (
            <>
              <Group label="Job search">
                <NavLink href="/app/today" icon={<CalendarIcon />}>
                  Today
                </NavLink>
                <NavLink href="/app/campaigns" icon={<SendIcon />}>
                  Applications
                </NavLink>
                <NavLink href="/jobs" icon={<BriefcaseIcon />}>
                  Jobs
                </NavLink>
                <NavLink href="/app/record" icon={<FileTextIcon />}>
                  Your record
                </NavLink>
              </Group>
              {hiring && (
                <Group label="Hiring">
                  <NavLink href="/hire/roles" icon={<ClipboardListIcon />}>
                    Roles
                  </NavLink>
                  <NavLink href="/hire/answers" icon={<MessageSquareIcon />}>
                    Answers
                  </NavLink>
                  <NavLink href="/hire/team" icon={<UsersIcon />}>
                    Team
                  </NavLink>
                </Group>
              )}
              <Group label="Control">
                <NavLink href="/control/costs" icon={<ReceiptIcon />}>
                  Costs
                </NavLink>
                <NavLink href="/trust" icon={<ShieldCheckIcon />}>
                  Trust
                </NavLink>
                <NavLink href="/app/settings" icon={<SlidersIcon />}>
                  Settings
                </NavLink>
              </Group>
            </>
          ) : (
            <Group>
              <NavLink href="/jobs" icon={<BriefcaseIcon />}>
                Jobs
              </NavLink>
              <NavLink href="/trust" icon={<ShieldCheckIcon />}>
                Trust
              </NavLink>
            </Group>
          )}
        </nav>
        <div className={styles.foot}>
          <div className={styles.ask}>
            <ChatOpener />
          </div>
          {local && signedIn && <ModelCell />}
          <div className={styles.tools}>
            <ThemeToggle />
            {!local && <AccountMenu />}
          </div>
        </div>
      </div>
    </>
  );
}
