import { LinkButton } from "@/components/ui/LinkButton";
import { Pill } from "@/components/ui/Pill";
import { PRODUCT_NAME } from "@/lib/product";
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

export default function Home() {
  return (
    <div className="container">
      <div className={styles.hero}>
        <section className={styles.intro} aria-labelledby="home-title">
          <p className={`t-label ${styles.kicker}`}>Open source, in progress</p>
          <h1 id="home-title" className={`t-display ${styles.title}`}>
            A career agent that shows its evidence.
          </h1>
          <p className={styles.lede}>
            {PRODUCT_NAME} is being built so that every match shows the evidence behind it, and nothing is sent in your
            name without your yes. Today it searches open jobs.
          </p>
          <div className={styles.actions}>
            <LinkButton href="/jobs" variant="primary">
              Search open jobs
            </LinkButton>
            <LinkButton href="/trust">See where your data goes</LinkButton>
          </div>
        </section>

        <section className={styles.ledger} aria-labelledby="today-title">
          <h2 id="today-title" className={`t-label ${styles.ledgerHead}`}>
            What works today
          </h2>
          <ul className={styles.rows}>
            {WORKING_TODAY.map((row) => (
              <li key={row.name}>
                <span className={styles.rowName}>{row.name}</span>
                <Pill tone="ok">Working</Pill>
                <span className={styles.rowText}>{row.text}</span>
              </li>
            ))}
          </ul>
          <p className={`t-small ${styles.note}`}>Everything else on the roadmap is planned and not built yet.</p>
        </section>
      </div>
    </div>
  );
}
