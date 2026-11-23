import Link from "next/link";
import states from "@/components/ui/states.module.css";
import { WarningIcon } from "@/components/icons/icons";

export type AiOffReason = "consent_required" | "providers_changed" | "ai_disabled";

/**
 * Why an AI feature is not available, in the one sentence that is true (FRONTEND_UI 3.0): you have not agreed, or this
 * installation has no model. Never both. Search, the record and the intake keep working, and the notice says so.
 */
export function AiOffNotice({ reason, reviewHref = "/app/settings?section=privacy" }: { reason: AiOffReason; reviewHref?: string }) {
  const needsConsent = reason !== "ai_disabled";
  return (
    <div className={`${states.panel} ${states.notice}`}>
      <span className={states.icon}>
        <WarningIcon size={20} />
      </span>
      <div className={states.body}>
        <p className="t-card">{needsConsent ? "Turn on AI processing to use this" : "AI features are off on this installation"}</p>
        {needsConsent ? (
          <>
            <p>
              {reason === "providers_changed"
                ? "AI features are off until you agree again, because the list of AI providers changed since you agreed."
                : "AI features are off because you have not agreed to AI processing."}{" "}
              The record, your constraints and search still work.
            </p>
            <Link href={reviewHref}>Review and agree</Link>
          </>
        ) : (
          <p>AI features are not set up on this server. Search and everything already saved still work.</p>
        )}
      </div>
    </div>
  );
}
