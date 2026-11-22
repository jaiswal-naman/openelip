import styles from "./button.module.css";

export type ButtonVariant = "primary" | "secondary" | "quiet";
export type ButtonSize = "sm" | "md";

/** The button class list, shared by the React Aria <button> and by links that look like buttons. */
export function buttonClass(variant: ButtonVariant = "secondary", size: ButtonSize = "md"): string {
  return `${styles.button} ${styles[variant]} ${styles[size]}`;
}

export const spinnerClass = styles.spinner;
