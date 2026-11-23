"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type FormEvent, useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import field from "@/components/ui/field.module.css";
import { LinkButton } from "@/components/ui/LinkButton";
import { TextField } from "@/components/ui/TextField";
import { useApi } from "@/lib/api/context";
import { ApiFailure, unwrap } from "@/lib/api/errors";
import { messageFor } from "@/lib/labels";
import { localKeys, localStatusQuery } from "./queries";

type Provider = "anthropic" | "openai";

const NOT_A_KEY = "That does not look like an API key. Paste it exactly as your provider shows it, with no spaces.";

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

  if (status.isPending) return <p role="status">Loading.</p>;
  if (status.isError) {
    return <p role="alert">Setup is for an installation running on your own computer (APP_MODE=local). It is not available here.</p>;
  }

  const model = status.data.model;
  const failure = save.error instanceof ApiFailure ? save.error : null;
  const problem = failure === null ? null : failure.code === "validation_failed" ? NOT_A_KEY : messageFor(failure);

  return (
    <section aria-labelledby="setup-title">
      <h1 id="setup-title" className="t-page">
        Set up your model
      </h1>
      <p>
        Paste an API key from your model provider. It is stored encrypted on this computer, and only its last four characters are ever
        shown again.
      </p>
      {model.key_set && model.key_last4 !== null && (
        <p role="status">
          A key ending in {model.key_last4} is in use ({model.source === "environment" ? "set in .env" : "saved here"}).
        </p>
      )}
      <form onSubmit={submit}>
        <div className={field.field}>
          <label htmlFor={providerId} className={field.label}>
            Provider
          </label>
          <select id={providerId} className={field.input} value={provider} onChange={(event) => setProvider(event.target.value as Provider)}>
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
          </select>
        </div>
        <TextField name="api_key" type="password" label="API key" autoComplete="off" isRequired error={problem} />
        <Button type="submit" variant="primary" isPending={save.isPending}>
          Save key
        </Button>
      </form>
      {model.provider === "fake" ? (
        <LinkButton href={next}>Skip for now, explore with demo data</LinkButton>
      ) : (
        <LinkButton href={next}>Skip for now</LinkButton>
      )}
    </section>
  );
}
