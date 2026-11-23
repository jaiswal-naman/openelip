import { useQuery } from "@tanstack/react-query";
import { useMe } from "@/features/auth/queries";
import { subprocessorsQuery } from "@/features/trust/queries";
import { useApi } from "@/lib/api/context";

/**
 * Where the signed-in person stands on AI processing. No model provider configured means nothing to agree to, so that
 * is checked first (the API answers 503 ai_disabled before it asks for a consent, 03-05). Then no agreement, then an
 * agreement given for a different list of providers (the wording version differs), then ready.
 */
export type AiGate = "loading" | "ready" | "consent_required" | "providers_changed" | "ai_disabled";

export function useAiConsent() {
  const api = useApi();
  const me = useMe();
  const subprocessors = useQuery(subprocessorsQuery(api));

  const consent = me.data?.consents.find((item) => item.purpose === "ai_processing") ?? null;
  const providers = (subprocessors.data?.items ?? []).filter((item) => item.kind === "model_provider");

  let gate: AiGate = "loading";
  if (me.data && subprocessors.data) {
    if (providers.length === 0) gate = "ai_disabled";
    else if (!consent?.granted) gate = "consent_required";
    else if (consent.text_version !== consent.current_text_version) gate = "providers_changed";
    else gate = "ready";
  }

  return { gate, consent, providers, error: me.error ?? subprocessors.error, refetch: subprocessors.refetch };
}
