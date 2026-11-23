import type { Citation, Proposal } from "./queries";
import type { components } from "@/lib/api/schema";

export type CampaignProposal = components["schemas"]["CampaignProposalView"];

/** What the panel knows about one reply while it streams (API.md 4.2). Built only by `reduceReply`. */
export type Reply = {
  text: string;
  citations: Record<string, Citation>;
  proposals: Proposal[];
  campaignProposals: CampaignProposal[];
  status: string | null; // the line shown while a tool runs
  constraintsApplied: boolean; // a job search ran, and the API applied the person's hard constraints
  done: boolean;
  finishReason: "complete" | "length" | "stopped" | "interrupted" | null;
  error: { code: string; retryable: boolean } | null;
};

export const newReply = (): Reply => ({
  text: "",
  citations: {},
  proposals: [],
  campaignProposals: [],
  status: null,
  constraintsApplied: false,
  done: false,
  finishReason: null,
  error: null,
});

const TOOL_STATUS: Record<string, string> = {
  get_record: "Reading your record...",
  list_matches: "Looking at your matches...",
  get_decision: "Looking at a decision...",
  search_jobs: "Searching jobs...",
  get_job: "Reading a job...",
  get_applications: "Looking at your applications...",
  propose_record_change: "Preparing a suggested change...",
  get_campaign: "Looking at a campaign...",
  get_dossier: "Reading a company dossier...",
  explain_block: "Finding out why...",
  propose_campaign: "Preparing a campaign suggestion...",
};

type Data = Record<string, unknown>;
const str = (value: unknown): string => (typeof value === "string" ? value : "");

/**
 * One stream event applied to a reply. A first connection replays from event 1; a reconnect sends `message.snapshot` first,
 * which replaces what was partial. `message.end` is always the last frame, and `error` is always followed by one.
 */
export function reduceReply(reply: Reply, event: string, data: Data): Reply {
  switch (event) {
    case "text.delta":
      return { ...reply, text: reply.text + str(data.text) };
    case "tool.start":
      return { ...reply, status: TOOL_STATUS[str(data.tool)] ?? "Working..." };
    case "tool.result":
      return { ...reply, status: null, constraintsApplied: reply.constraintsApplied || str(data.tool) === "search_jobs" };
    case "citation": {
      const citation = data as unknown as Citation;
      return { ...reply, citations: { ...reply.citations, [citation.alias]: citation } };
    }
    case "proposal": {
      const proposal = data as unknown as Proposal;
      return reply.proposals.some((p) => p.event_id === proposal.event_id) ? reply : { ...reply, proposals: [...reply.proposals, proposal] };
    }
    case "campaign_proposal": {
      const offer = data as unknown as CampaignProposal;
      return reply.campaignProposals.some((p) => p.job_id === offer.job_id) ? reply : { ...reply, campaignProposals: [...reply.campaignProposals, offer] };
    }
    case "message.snapshot": {
      const citations = Array.isArray(data.citations) ? (data.citations as Citation[]) : [];
      const proposals = Array.isArray(data.proposals) ? (data.proposals as Proposal[]) : [];
      const offers = Array.isArray(data.campaign_proposals) ? (data.campaign_proposals as CampaignProposal[]) : [];
      return { ...reply, text: str(data.text), citations: Object.fromEntries(citations.map((c) => [c.alias, c])), proposals, campaignProposals: offers };
    }
    case "error":
      return { ...reply, status: null, error: { code: str(data.code), retryable: data.is_retryable === true } };
    case "message.end": {
      const reason = str(data.finish_reason);
      const known = reason === "complete" || reason === "length" || reason === "stopped" || reason === "interrupted";
      return { ...reply, status: null, done: true, finishReason: known ? reason : "interrupted" };
    }
    default:
      return reply;
  }
}
