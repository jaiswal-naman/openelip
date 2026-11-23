import type { CampaignDetail } from "./queries";

/** An invented campaign detail for component tests (every name and address is made up). */
export function detail(over: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    id: "c1",
    job: { id: 42, title: "Platform Engineer", company_name: "Globex Demo", apply_url: "https://jobs.globex.example/apply/42" },
    stage: "researching",
    from_introduction: false,
    introduction: null,
    paused_at: null,
    pause_reason: null,
    pause_text: null,
    scam_signals: [],
    closed_at: null,
    closed_reason: null,
    budget_usd: "0.750000",
    spent_usd: "0.120000",
    follow_up_after_days: 7,
    follow_up_email: null,
    created_at: "2026-11-23T09:00:00Z",
    server_time: "2026-11-23T10:00:00Z",
    next_step: null,
    steps: [],
    dossier: null,
    fit: null,
    packet: null,
    prep: null,
    follow_ups: [],
    pending_approvals: [],
    ...over,
  };
}
