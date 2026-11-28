"use client";

import { useState } from "react";
import { Group, Input, Label, NumberField } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { TextField } from "@/components/ui/TextField";
import { NativeSelect } from "@/ui/patterns/NativeSelect";
import { describeFailure } from "@/lib/api/problems";
import { WORK_ARRANGEMENT_LABELS } from "@/lib/labels";
import { type RoleView, usePatchRole } from "./api";
import { Zone } from "./Zone";
import styles from "./hiring.module.css";

const CURRENCIES = ["USD", "EUR", "GBP", "INR", "CAD", "AUD", "CHF", "SGD", "JPY"];
const ARRANGEMENTS = ["onsite", "hybrid", "remote"] as const;

function Money({ label, value, onChange }: { label: string; value: number | null; onChange: (next: number | null) => void }) {
  return (
    <NumberField
      className={field.field}
      value={value ?? Number.NaN}
      onChange={(next) => onChange(Number.isNaN(next) ? null : next)}
      minValue={0}
      formatOptions={{ useGrouping: true, maximumFractionDigits: 0 }}
    >
      <Label className={field.label}>{label}</Label>
      <Group>
        <Input className={field.input} />
      </Group>
    </NumberField>
  );
}

/** The pay range (annual, whole units) and where the role is. Saved with PATCH; a minimum above the maximum is refused here. */
export function PayRange({ role }: { role: RoleView }) {
  const patch = usePatchRole(role.id);
  const [currency, setCurrency] = useState(role.pay_range?.currency ?? "USD");
  const [min, setMin] = useState<number | null>(role.pay_range?.min ?? null);
  const [max, setMax] = useState<number | null>(role.pay_range?.max ?? null);
  const [country, setCountry] = useState(role.country ?? "");
  const [city, setCity] = useState(role.city ?? "");
  const [arrangements, setArrangements] = useState<string[]>(role.work_arrangements);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const currencies = CURRENCIES.includes(currency) ? CURRENCIES : [currency, ...CURRENCIES];

  function save() {
    setSaved(false);
    if ((min === null) !== (max === null)) return setProblem("Give both a minimum and a maximum, or neither.");
    if (min !== null && max !== null && min > max) return setProblem("The minimum cannot be above the maximum.");
    if (country !== "" && !/^[A-Za-z]{2}$/.test(country)) return setProblem("Use the two-letter country code, for example IN.");
    setProblem(null);
    patch.mutate(
      {
        pay_range: min !== null && max !== null ? { min, max, currency } : null,
        country: country === "" ? null : country.toUpperCase(),
        city: city.trim() === "" ? null : city.trim(),
        ...(arrangements.length > 0 ? { work_arrangements: arrangements as RoleView["work_arrangements"] } : {}),
      },
      { onSuccess: () => setSaved(true) },
    );
  }

  return (
    <Zone id="pay-heading" number={3} title="Pay range (required to publish)">
      <div className={styles.fields}>
        <NativeSelect label="Currency" value={currency} onChange={setCurrency} options={currencies.map((code) => ({ value: code, label: code }))} />
        <Money label="Minimum a year" value={min} onChange={setMin} />
        <Money label="Maximum a year" value={max} onChange={setMax} />
      </div>
      <h3 className={`t-label ${styles.subhead}`}>Location</h3>
      <div className={styles.fields}>
        <TextField label="Country (two letters)" value={country} onChange={setCountry} maxLength={2} />
        <TextField label="City" value={city} onChange={setCity} maxLength={120} />
      </div>
      {/* Native checkboxes, as on the record screens: they show their state and are a full-size target. */}
      <fieldset className={styles.choices}>
        <legend className={field.label}>Work arrangements</legend>
        <div className={styles.row}>
          {ARRANGEMENTS.map((value) => (
            <label key={value} className={styles.choice}>
              <input
                type="checkbox"
                checked={arrangements.includes(value)}
                onChange={(event) =>
                  setArrangements(event.target.checked ? [...arrangements, value] : arrangements.filter((item) => item !== value))
                }
              />
              <span>{WORK_ARRANGEMENT_LABELS[value]}</span>
            </label>
          ))}
        </div>
      </fieldset>
      {(problem ?? (patch.isError ? describeFailure(patch.error) : null)) && (
        <p role="alert" className={styles.problem}>
          {problem ?? describeFailure(patch.error)}
        </p>
      )}
      {saved && <p role="status">Saved.</p>}
      <div className={styles.row}>
        <Button isPending={patch.isPending} onPress={save}>
          Save pay and location
        </Button>
      </div>
    </Zone>
  );
}

/** The same facts when the role can no longer change. */
export function PayRangeReadOnly({ role }: { role: RoleView }) {
  const pay = role.pay_range;
  return (
    <Zone id="pay-heading" number={3} title="Pay range">
      <p>{pay ? `${pay.currency} ${pay.min.toLocaleString("en-US")} to ${pay.max.toLocaleString("en-US")} a year` : "Not set"}</p>
      <p className={styles.muted}>{[role.city, role.country].filter(Boolean).join(", ") || "No location set"}</p>
    </Zone>
  );
}
