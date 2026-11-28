import { CheckIcon } from "@/components/icons/icons";
import styles from "./chip.module.css";

/**
 * The APPROVED chip: a black pill with a check mark. `landing` plays the one 180ms settle, for the moment the person has
 * just approved in this view; a chip that was already there when the page opened is simply drawn.
 */
export function ApprovedChip({ landing = false }: { landing?: boolean }) {
  return (
    <span className={landing ? `${styles.chip} ${styles.landing}` : styles.chip}>
      <CheckIcon />
      Approved
    </span>
  );
}
