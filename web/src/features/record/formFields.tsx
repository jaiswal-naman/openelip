"use client";

import { useId } from "react";
import { Label, Text, TextArea, TextField as AriaTextField } from "react-aria-components";
import { ChevronDownIcon } from "@/components/icons/icons";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { COUNTRIES, countryName } from "@/features/jobs/countries";
import styles from "./constraints.module.css";

const capitalized = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** A few options of which any number may be ticked: a native fieldset, so the group has a name and every box a label. */
export function CheckGroup({
  legend,
  options,
  value,
  onChange,
  error,
}: {
  legend: string;
  options: { value: string; label: string }[];
  value: string[];
  onChange: (next: string[]) => void;
  error?: string | null;
}) {
  const errorId = useId();
  return (
    <fieldset className={styles.group} aria-describedby={error ? errorId : undefined}>
      <legend className={field.label}>{legend}</legend>
      <div className={styles.options}>
        {options.map((option) => (
          <label key={option.value} className={styles.option}>
            <input
              type="checkbox"
              checked={value.includes(option.value)}
              onChange={(event) => onChange(event.target.checked ? [...value, option.value] : value.filter((item) => item !== option.value))}
            />
            <span>{capitalized(option.label)}</span>
          </label>
        ))}
      </div>
      {error && (
        <p id={errorId} className={field.error}>
          {error}
        </p>
      )}
    </fieldset>
  );
}

/** Any number of countries: a select that adds the one chosen to a list, each of which can be removed. */
export function CountryPicker({
  label,
  value,
  onChange,
  error,
}: {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  error?: string | null;
}) {
  const id = useId();
  const errorId = useId();
  return (
    <div className={field.field}>
      <label htmlFor={id} className={field.label}>
        {label}
      </label>
      <div className={styles.selectWrap}>
        <select
          id={id}
          value=""
          className={`${field.input} ${styles.select}`}
          aria-describedby={error ? errorId : undefined}
          aria-invalid={error ? true : undefined}
          onChange={(event) => event.target.value && onChange([...value, event.target.value])}
        >
          <option value="">{value.length === 0 ? "Choose a country" : "Add another country"}</option>
          {COUNTRIES.filter((country) => !value.includes(country.code)).map((country) => (
            <option key={country.code} value={country.code}>
              {country.name}
            </option>
          ))}
        </select>
        <span className={styles.selectIcon}>
          <ChevronDownIcon />
        </span>
      </div>
      {error && (
        <p id={errorId} className={field.error}>
          {error}
        </p>
      )}
      {value.length > 0 && (
        <ul className={styles.chips} aria-label={`${label}, chosen`}>
          {value.map((code) => (
            <li key={code} className={styles.chip}>
              <span>{countryName(code)}</span>
              <Button variant="quiet" size="sm" aria-label={`Remove ${countryName(code)}`} onPress={() => onChange(value.filter((item) => item !== code))}>
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Several short values, one per line. A line holds a comma safely, which "Washington, D.C." and "Acme, Inc." need. */
export function LinesField({
  label,
  description,
  value,
  onChange,
  error,
}: {
  label: string;
  description?: string;
  value: string;
  onChange: (next: string) => void;
  error?: string | null;
}) {
  return (
    <AriaTextField className={field.field} value={value} onChange={onChange} isInvalid={Boolean(error)}>
      <Label className={field.label}>{label}</Label>
      <TextArea className={`${field.input} ${styles.textarea}`} rows={3} />
      {description && (
        <Text slot="description" className={field.hint}>
          {description}
        </Text>
      )}
      {error && (
        <Text slot="errorMessage" className={field.error}>
          {error}
        </Text>
      )}
    </AriaTextField>
  );
}

/** A native select for a short list of choices, drawn like the other fields. */
export function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  options: { value: string; label: string }[];
}) {
  const id = useId();
  return (
    <div className={field.field}>
      <label htmlFor={id} className={field.label}>
        {label}
      </label>
      <div className={styles.selectWrap}>
        <select id={id} value={value} className={`${field.input} ${styles.select}`} onChange={(event) => onChange(event.target.value)}>
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <span className={styles.selectIcon}>
          <ChevronDownIcon />
        </span>
      </div>
    </div>
  );
}
