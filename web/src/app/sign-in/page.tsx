import type { Metadata } from "next";
import { redirect } from "next/navigation";
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

  return (
    <div className="container page">
      <section className={styles.card} aria-labelledby="sign-in-title">
        <div className={styles.head}>
          <h1 id="sign-in-title" className="t-page">
            Sign in
          </h1>
          <p className={styles.note}>We will email you a one-time code. There is no password.</p>
        </div>
        <SignInForm next={next} />
        <p className={`t-small ${styles.note}`}>The first time you sign in with an address, that creates your account.</p>
      </section>
    </div>
  );
}
