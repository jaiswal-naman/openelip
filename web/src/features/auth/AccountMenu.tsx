"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Button, Header, Menu, MenuItem, MenuSection, MenuTrigger, Popover } from "react-aria-components";
import { ChevronDownIcon, LogOutIcon, UserIcon } from "@/components/icons/icons";
import { buttonClass } from "@/components/ui/buttonStyles";
import menu from "@/components/ui/menu.module.css";
import styles from "./account.module.css";
import { useMe } from "./queries";
import { useSignOut } from "./useSignOut";

/** The header's account slot: a way to sign in, or the signed-in person's email with a menu to sign out. */
export function AccountMenu() {
  const me = useMe();
  const signOut = useSignOut();
  const pathname = usePathname();
  const search = useSearchParams().toString();

  // No account, or not known yet because the API could not answer: offering sign-in is the safe thing to show.
  if (!me.data) return <SignInLink from={pathname} search={search} />;

  const { email } = me.data;
  return (
    <div className={styles.account}>
      <MenuTrigger onOpenChange={(isOpen) => isOpen && signOut.reset()}>
        <Button className={menu.trigger} aria-label={`Account: ${email}`}>
          <UserIcon />
          <span className={styles.email}>{email}</span>
          <ChevronDownIcon />
        </Button>
        <Popover className={menu.popover} placement="bottom end">
          <Menu
            className={menu.menu}
            aria-label="Account"
            onAction={(key) => {
              if (key === "sign-out") signOut.mutate();
            }}
          >
            <MenuSection>
              <Header className={menu.heading}>
                Signed in as <strong>{email}</strong>
              </Header>
              <MenuItem id="today" href="/app/today" textValue="Today" className={menu.item}>
                <span>Today</span>
              </MenuItem>
              <MenuItem id="applications" href="/app/campaigns" textValue="Applications" className={menu.item}>
                <span>Applications</span>
              </MenuItem>
              <MenuItem id="record" href="/app/record" textValue="Your record" className={menu.item}>
                <span>Your record</span>
              </MenuItem>
              <MenuItem id="settings" href="/app/settings?section=privacy" textValue="Settings" className={menu.item}>
                <span>Settings</span>
              </MenuItem>
              <MenuItem id="sign-out" textValue="Sign out" className={menu.item}>
                <LogOutIcon />
                <span>Sign out</span>
              </MenuItem>
            </MenuSection>
          </Menu>
        </Popover>
      </MenuTrigger>
      {signOut.isError && (
        <p role="alert" className={styles.problem}>
          Could not sign out. You are still signed in. Try again.
        </p>
      )}
    </div>
  );
}

/** Sign in, and come back to this page afterwards. */
function SignInLink({ from, search }: { from: string; search: string }) {
  const here = from.startsWith("/sign-in") ? null : `${from}${search ? `?${search}` : ""}`;
  const href = here === null ? "/sign-in" : `/sign-in?next=${encodeURIComponent(here)}`;
  return (
    <Link href={href} className={buttonClass("secondary", "sm")}>
      Sign in
    </Link>
  );
}
