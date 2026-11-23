import type { Metadata } from "next";
import { LinkButton } from "@/components/ui/LinkButton";
import styles from "@/features/privacy/privacy.module.css";

export const metadata: Metadata = { title: "Account deleted" };

/**
 * Where a person lands after deleting their account (TRU-02). They are already signed out, so nothing here needs a
 * session and nothing is read from the API: it only says what happened and what could not be reached.
 */
export default function AccountDeletedPage() {
  return (
    <div className="container page">
      <section className={styles.farewell} aria-labelledby="deleted-title">
        <h1 id="deleted-title" className="t-page">
          Your account is deleted
        </h1>
        <p>You are signed out. Your record, documents, facts, constraints, preferences, change log and consents are being erased now.</p>
        <p>
          Copies made outside this system, for example an email that has already been delivered to someone else, cannot be reached by
          deletion. If this server can send email, you get one message to confirm.
        </p>
        <LinkButton href="/">Back to the home page</LinkButton>
      </section>
    </div>
  );
}
