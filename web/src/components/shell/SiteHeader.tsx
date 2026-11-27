import styles from "./header.module.css";
import { RegisterNav } from "./RegisterNav";

/**
 * The workspace sidebar: the pages down the left side. On a narrow screen it folds into a top bar with a Menu button. In local
 * mode (APP_MODE=local, read on the server) there is one owner and no account to switch, so the account menu gives way to the
 * model-key cell and the hiring desk is always listed.
 */
export function SiteHeader() {
  return (
    <>
      <a href="#main" className={styles.skip}>
        Skip to main content
      </a>
      <header className={styles.register}>
        <RegisterNav local={process.env.APP_MODE === "local"} />
      </header>
    </>
  );
}
