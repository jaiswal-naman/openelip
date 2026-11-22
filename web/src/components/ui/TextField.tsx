"use client";

import type { ReactNode } from "react";
import { FieldError, Input, Label, Text, TextField as AriaTextField, type TextFieldProps } from "react-aria-components";
import styles from "./field.module.css";

type Props = Omit<TextFieldProps, "className" | "children"> & {
  label: string;
  description?: ReactNode;
  placeholder?: string;
  /** Style the input as the spaced mono code field. */
  code?: boolean;
};

/** A labelled text field. The label is always visible; errors appear in text under the field. */
export function TextField({ label, description, placeholder, code, ...props }: Props) {
  return (
    <AriaTextField {...props} className={styles.field}>
      <Label className={styles.label}>{label}</Label>
      <Input className={code ? `${styles.input} ${styles.code}` : styles.input} placeholder={placeholder} />
      {description && (
        <Text slot="description" className={styles.hint}>
          {description}
        </Text>
      )}
      <FieldError className={styles.error} />
    </AriaTextField>
  );
}
