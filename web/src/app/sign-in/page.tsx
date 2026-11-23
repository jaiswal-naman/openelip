import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { meQuery } from "@/features/auth/queries";
import { safeNextPath } from "@/features/auth/safeNext";
import { SignInForm } from "@/features/auth/SignInForm";
import styles from "@/features/auth/signin.module.css";
import { serverApi } from "@/lib/api/server";
import { makeQueryClient } from "@/lib/query";

export const metadata: Metadata = { title: "Sign in" };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function SignInPage({ searchParams }: { searchParams: SearchParams }) {
  const next = safeNextPath((await searchParams).next);

  // Someone who is already signed in has nothing to do here. If the API cannot say, show the form.
  const me = await makeQueryClient()
    .fetchQuery(meQuery(await serverApi()))
    .catch(() => null);
  if (me) redirect(next);

  // Local mode has no sign-in (the setting the API reads): /local-session opens the owner's session and continues.
  if (process.env.APP_MODE === "local") redirect(`/local-session?next=${encodeURIComponent(next)}`);

  return (
    <div className="container page">
      <TitleBlock title="Sign in" lines={<p>We will email you a one-time code. There is no password.</p>} />
      <div className={styles.card}>
        <SignInForm next={next} />
        <p className={`t-small ${styles.note}`}>The first time you sign in with an address, that creates your account.</p>
      </div>
    </div>
  );
}
