import { redirect } from "next/navigation";
import { LinkButton } from "@/components/ui/LinkButton";
import ledger from "@/components/ui/ledger.module.css";
import { Pill } from "@/components/ui/Pill";
import { TitleBlock } from "@/components/ui/TitleBlock";
import { recordQuery } from "@/features/record/api";
import { hasReadResume } from "@/features/record/hasReadResume";
import { serverApi } from "@/lib/api/server";
import { PRODUCT_NAME } from "@/lib/product";
import { makeQueryClient } from "@/lib/query";
import styles from "./home.module.css";

// Only what ships today is listed here (FRONTEND_UI 3.22). Add a row when a feature is built, not before.
const WORKING_TODAY = [
  {
    name: "Job index",
    text: "Open jobs from public Greenhouse, Lever and Ashby boards, searched by keyword, country and remote.",
  },
  { name: "Sign-in", text: "An emailed one-time code. There is no password to keep." },
  { name: "Costs", text: "Every model call is recorded with its cost, and you can read your own." },
  { name: "Trust", text: "The model providers and the email host this server uses, listed from its running configuration." },
];

/**
 * Where a person in local mode starts. First launch goes API key (the /setup step, which /local-session sends a stack
 * with no key to), then the resume, then Today: a person whose record has no resume read yet goes on to onboarding, and
 * everyone else to Today. If the API cannot say, Today: not knowing is no reason to send anyone through the setup again.
 */
async function localStart(): Promise<string> {
  const record = await makeQueryClient()
    .fetchQuery(recordQuery(await serverApi()))
    .catch(() => null);
  return record !== null && !hasReadResume(record) ? "/onboarding" : "/app/today";
}

export default async function Home() {
  // Local mode is one person's own workspace: the front page is their day, not a page about the project.
  if (process.env.APP_MODE === "local") redirect(await localStart());
  return (
    <div className="container page">
      <TitleBlock
        title="A career agent that shows its evidence."
        lines={
          <p className={styles.lede}>
            {PRODUCT_NAME} is being built so that every match shows the evidence behind it, and nothing is sent in your name without your yes. Today it
            searches open jobs.
          </p>
        }
        cells={[
          { label: "Status", value: "Open source, in progress" },
          { label: "Working today", value: WORKING_TODAY.length },
        ]}
        actions={
          <>
            <LinkButton href="/jobs" variant="primary">
              Search open jobs
            </LinkButton>
            <LinkButton href="/trust">See where your data goes</LinkButton>
          </>
        }
      />

      <section className={styles.today} aria-labelledby="today-title">
        <h2 id="today-title" className="t-section">
          What works today
        </h2>
        <table className={ledger.table}>
          <caption className="sr-only">The features that work today</caption>
          <thead>
            <tr>
              <th scope="col">Feature</th>
              <th scope="col">What it does</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {WORKING_TODAY.map((row) => (
              <tr key={row.name}>
                <th scope="row">{row.name}</th>
                <td data-label="What it does">{row.text}</td>
                <td data-label="Status">
                  <Pill tone="ok">Working</Pill>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`t-small ${styles.note}`}>Everything else on the roadmap is planned and not built yet.</p>
      </section>
    </div>
  );
}
