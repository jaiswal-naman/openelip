"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useApi } from "@/lib/api/context";
import { type DismissReason, type FeedbackBody, giveFeedback } from "./queries";

type State = { isSaved: boolean; isDismissed: boolean; hasProposal: boolean };

/**
 * Save, unsave and dismiss for one match (MAT-08). These are local and reversible, so they are optimistic and roll back on
 * a failure (FRONTEND_UI 4.9): the control changes at once, and if the server refuses it goes back and says so. The newest
 * event decides, so saving a dismissed job takes the dismissal back. A dismissal can come back with a proposed record
 * change (`hasProposal`); that changes nothing until the person confirms it on their record page.
 */
export function useFeedback(decisionId: string, initial: { is_saved: boolean; is_dismissed: boolean }) {
  const api = useApi();
  const [state, setState] = useState<State>({ isSaved: initial.is_saved, isDismissed: initial.is_dismissed, hasProposal: false });
  // Only a failed save or unsave is kept here. A failed dismissal is reported to the dialog that asked for it, which is open.
  const [saveFailure, setSaveFailure] = useState<unknown>(null);
  const send = useMutation({
    mutationFn: (body: FeedbackBody) => giveFeedback(api, decisionId, body),
    onMutate: (body) => {
      const before = state;
      setSaveFailure(null);
      setState({ isSaved: body.kind === "save", isDismissed: body.kind === "dismiss", hasProposal: false });
      return { before };
    },
    onError: (error, body, context) => {
      if (context) setState(context.before);
      if (body.kind !== "dismiss") setSaveFailure(error);
    },
    onSuccess: (result) =>
      setState({ isSaved: result.is_saved, isDismissed: result.is_dismissed, hasProposal: result.proposal_event_id !== null }),
  });

  return {
    ...state,
    isPending: send.isPending,
    error: saveFailure,
    toggleSave: () => send.mutate({ kind: state.isSaved ? "unsave" : "save" }),
    /** Resolves when the server has recorded it, so a dialog can close; rejects with the failure, so it can say what went wrong. */
    dismiss: (reason: DismissReason, comment: string) =>
      send.mutateAsync({ kind: "dismiss", reason, comment: comment.trim() === "" ? null : comment.trim() }),
  };
}
