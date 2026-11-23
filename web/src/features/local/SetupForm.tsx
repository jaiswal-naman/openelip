"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type FormEvent, useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { LinkButton } from "@/components/ui/LinkButton";
import { TextField } from "@/components/ui/TextField";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { useApi } from "@/lib/api/context";
import { ApiFailure, unwrap } from "@/lib/api/errors";
import { messageFor } from "@/lib/labels";
import { localKeys, localStatusQuery } from "./queries";
import styles from "./setup.module.css";

type Provider = "anthropic" | "openai";

const PROVIDER_NAMES: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", fake: "Demo answers" };

const NOT_A_KEY = "That does not look like an API key. Paste it exactly as your provider shows it, with no spaces.";

/** The sheet's title block. Its cells say what the stack reports about the model, and are left out until it has answered. */
function Head({ model }: { model?: { provider: string; key_set: boolean; key_last4: string | null } }) {
  const cells: TitleCell[] = [];
  if (model !== undefined) {
    cells.push({ label: "Provider", value: PROVIDER_NAMES[model.provider] ?? model.provider });
    // The demo provider answers from recordings and has no key to set
    if (model.provider !== "fake") {
      cells.push(
        model.key_set
          ? { label: "Key", value: model.key_last4 === null ? "Set" : `Set, ends ${model.key_last4}`, tone: "ok" }
          : { label: "Key", value: "Not set", tone: "redline" },
      );
    }
  }
  return <TitleBlock title="Set up your model" lines={<p>Your agents use the model you choose, with your own API key.</p>} cells={cells} />;
}

/**
 * Local mode: paste the model API key (PUT /v1/settings/model-key). The key goes to the API and is never shown again, only
 * its last four characters. When the stack runs the fake provider there is nothing to set up, and the person may go on and
 * explore with the recorded demo answers. `next` is already checked by the page.
 */
export function SetupForm({ next }: { next: string }) {
  const api = useApi();
  const router = useRouter();
  const queryClient = useQueryClient();
  const providerId = useId();
  const [provider, setProvider] = useState<Provider>("anthropic");
  const status = useQuery(localStatusQuery(api));

  const save = useMutation({
    mutationFn: (body: { provider: Provider; api_key: string }) => unwrap(api.PUT("/v1/settings/model-key", { body })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: localKeys.status });
      router.replace(next);
      router.refresh();
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate({ provider, api_key: String(new FormData(event.currentTarget).get("api_key") ?? "").trim() });
  }

  if (status.isPending) {
    return (
      <>
        <Head />
        <p role="status">Loading.</p>
      </>
    );
  }
  if (status.isError) {
    return (
      <>
        <Head />
        <p role="alert" className={styles.problem}>
          Setup is for an installation running on your own computer (APP_MODE=local). It is not available here.
        </p>
      </>
    );
  }

  const model = status.data.model;
  const failure = save.error instanceof ApiFailure ? save.error : null;
  const problem = failure === null ? null : failure.code === "validation_failed" ? NOT_A_KEY : messageFor(failure);

  return (
    <>
      <Head model={model} />
      <section className={styles.sheet} aria-label="Model key">
        {model.key_set && model.key_last4 !== null && (
          <p role="status" className={styles.inUse}>
            A key ending in {model.key_last4} is in use ({model.source === "environment" ? "set in .env" : "saved here"}).
          </p>
        )}
        <form onSubmit={submit} className={styles.form}>
          <div className={field.field}>
            <label htmlFor={providerId} className={field.label}>
              Provider
            </label>
            <select id={providerId} className={field.input} value={provider} onChange={(event) => setProvider(event.target.value as Provider)}>
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
            </select>
          </div>
          <TextField
            name="api_key"
            type="password"
            label="API key"
            description="Paste a key from your model provider. It is sealed in the local database on this computer, and only its last four characters are ever shown again."
            autoComplete="off"
            isRequired
            error={problem}
          />
          <div className={styles.buttons}>
            <Button type="submit" variant="primary" isPending={save.isPending}>
              Save key
            </Button>
            <LinkButton href={next}>{model.provider === "fake" ? "Skip for now, explore with demo data" : "Skip for now"}</LinkButton>
          </div>
        </form>
      </section>
    </>
  );
}
