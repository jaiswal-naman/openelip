"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import { TimeZoneSelect } from "@/ui/patterns/TimeZoneSelect";
import { describeFailure } from "@/lib/api/problems";
import { useCreateOrganization } from "./api";
import styles from "./hiring.module.css";

/** "Create your organization to post roles." (FRONTEND_UI 3.12, HIR-01). The creator becomes its owner; then /hire/team. */
export function CreateOrganization() {
  const router = useRouter();
  const create = useCreateOrganization();
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");

  return (
    <section aria-labelledby="create-org" className={styles.paper}>
      <h2 id="create-org" className="t-section">
        Create your organization to post roles.
      </h2>
      <Form
        className={styles.stack}
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim() === "") return;
          create.mutate({ name: name.trim(), timezone }, { onSuccess: () => router.push("/hire/team") });
        }}
      >
        <TextField label="Organization name" value={name} onChange={setName} isRequired maxLength={120} />
        <TimeZoneSelect label="Time zone" value={timezone} onChange={setTimezone} />
        <p className={`t-small ${styles.muted}`}>Answers are due five business days after a candidate accepts, counted in this time zone.</p>
        {create.isError && (
          <p role="alert" className={styles.problem}>
            {describeFailure(create.error)} Nothing was created.
          </p>
        )}
        <div className={styles.row}>
          <Button type="submit" variant="primary" isPending={create.isPending}>
            Create organization
          </Button>
        </div>
      </Form>
    </section>
  );
}
