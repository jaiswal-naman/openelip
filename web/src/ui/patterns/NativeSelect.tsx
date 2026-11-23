"use client";

import { useId } from "react";
import { ChevronDownIcon } from "@/components/icons/icons";
import field from "@/components/ui/field.module.css";
import styles from "./NativeSelect.module.css";

/**
 * A labelled choice from a short or long list, drawn like the other fields. It is the browser's own `<select>`: the label,
 * the keyboard, type-ahead and the phone's picker all come with it, which is why it is used for the day of the week and the
 * time zone (a list of about 400 names) instead of a custom listbox.
 */
export function NativeSelect({
  label,
  value,
  onChange,
  options,
  hint,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  options: { value: string; label: string }[];
  hint?: string;
}) {
  const id = useId();
  const hintId = useId();
  return (
    <div className={field.field}>
      <label htmlFor={id} className={field.label}>
        {label}
      </label>
      <div className={styles.wrap}>
        <select
          id={id}
          value={value}
          className={`${field.input} ${styles.select}`}
          aria-describedby={hint ? hintId : undefined}
          onChange={(event) => onChange(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <span className={styles.icon}>
          <ChevronDownIcon />
        </span>
      </div>
      {hint && (
        <p id={hintId} className={field.hint}>
          {hint}
        </p>
      )}
    </div>
  );
}
