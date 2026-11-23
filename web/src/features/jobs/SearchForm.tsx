"use client";

import { useRouter } from "next/navigation";
import type { FormEvent } from "react";
import { Form, Label, Radio, RadioGroup } from "react-aria-components";
import { ChevronDownIcon } from "@/components/icons/icons";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { TextField } from "@/components/ui/TextField";
import { COUNTRIES } from "./countries";
import { MAX_QUERY_LENGTH, parseSearch, remoteFilterOf, type SearchParams, searchToQueryString } from "./jobs";
import styles from "./jobs.module.css";

/**
 * Keyword, country and remote. It is a plain GET form, so it works as a link even before scripts load; with scripts it
 * pushes the same URL through the router. The URL is the state: reload, share and the back button all work.
 */
export function SearchForm({ params }: { params: SearchParams }) {
  const router = useRouter();

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next = parseSearch({
      q: String(data.get("q") ?? ""),
      country: String(data.get("country") ?? ""),
      remote: String(data.get("remote") ?? ""),
    });
    const query = searchToQueryString(next);
    router.push(query === "" ? "/jobs" : `/jobs?${query}`);
  }

  return (
    <div className={styles.toolbar}>
      <Form action="/jobs" method="get" onSubmit={search} className={styles.form} aria-label="Search open jobs">
        <div className={styles.queryField}>
          <TextField name="q" type="search" label="Search open jobs" defaultValue={params.q} maxLength={MAX_QUERY_LENGTH} />
        </div>
        <div className={styles.go}>
          <Button type="submit" variant="primary">
            Search
          </Button>
        </div>
        <div className={styles.filters}>
          <div className={field.field}>
            <label htmlFor="country" className={field.label}>
              Country
            </label>
            <div className={styles.selectWrap}>
              <select
                id="country"
                name="country"
                defaultValue={params.country ?? ""}
                className={`${field.input} ${styles.select}`}
                aria-describedby="country-note"
              >
                <option value="">Any country</option>
                {COUNTRIES.map((country) => (
                  <option key={country.code} value={country.code}>
                    {country.name}
                  </option>
                ))}
              </select>
              <span className={styles.selectIcon}>
                <ChevronDownIcon />
              </span>
            </div>
          </div>
          <RadioGroup name="remote" defaultValue={remoteFilterOf(params.remote)} className={styles.group}>
            <Label className={field.label}>Remote</Label>
            <div className={styles.segmented}>
              <Radio value="any" className={styles.segment}>
                Any
              </Radio>
              <Radio value="true" className={styles.segment}>
                Remote only
              </Radio>
              <Radio value="false" className={styles.segment}>
                Not remote
              </Radio>
            </div>
          </RadioGroup>
        </div>
        <p id="country-note" className={`t-small ${styles.note}`}>
          A country filter matches only jobs whose country could be read from the posting; the rest are left out.
        </p>
      </Form>
    </div>
  );
}
