import { INDEPENDENCE_STATEMENT } from "@/lib/product";
import styles from "./footer.module.css";

export function SiteFooter() {
  return (
    <footer className={styles.footer}>
      <div className="container">
        <p className="t-small muted">{INDEPENDENCE_STATEMENT}</p>
      </div>
    </footer>
  );
}
