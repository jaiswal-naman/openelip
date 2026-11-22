"use client";

import type { ReactNode, Ref } from "react";
import { FieldError, Input, Label, Text, TextField as AriaTextField, type TextFieldProps } from "react-aria-components";
import styles from "./field.module.css";

type Props = Omit<TextFieldProps, "className" | "children"> & {
  label: string;
  description?: ReactNode;
  placeholder?: string;
  /** Style the input as the spaced mono code field. */
  code?: boolean;
  /** The input itself, for moving focus to it after a failed attempt. */
  inputRef?: Ref<HTMLInputElement>;
  /**
   * What is wrong with the value, from the last attempt. It stays until the next attempt answers, instead of vanishing when
   * the field is edited or loses focus: a message that disappears when you press the button moves the button, and the click is lost.
   */
  error?: string | null;
};

/** A labelled text field. The label is always visible; errors appear in text under the field. */
export function TextField({ label, description, placeholder, code, inputRef, error, ...props }: Props) {
  return (
    <AriaTextField {...props} isInvalid={Boolean(error)} className={styles.field}>
      <Label className={styles.label}>{label}</Label>
      <Input ref={inputRef} className={code ? `${styles.input} ${styles.code}` : styles.input} placeholder={placeholder} />
      {description && (
        <Text slot="description" className={styles.hint}>
          {description}
        </Text>
      )}
      <FieldError className={styles.error}>{error}</FieldError>
    </AriaTextField>
  );
}
