"use client";

import { useQuery } from "@tanstack/react-query";
import { TitleBlock, type TitleCell } from "@/components/ui/TitleBlock";
import { applicationsQuery } from "@/features/applications/queries";
import { useApi } from "@/lib/api/context";
import { boardQuery } from "./queries";

/**
 * The header of the Applications page. Its cells are counted from the board and the application list the page loads
 * below it (the same queries, so nothing is asked twice); until they arrive the header shows its heading alone.
 */
export function CampaignsTitle() {
  const api = useApi();
  const board = useQuery(boardQuery(api, false));
  const applications = useQuery(applicationsQuery(api));

  const cells: TitleCell[] = [];
  if (board.data !== undefined) {
    const cards = board.data.columns.flatMap((column) => column.campaigns.map((card) => ({ card, stage: column.stage })));
    const waiting = cards.filter(({ card, stage }) => card.pending_approvals > 0 && stage !== "researching" && stage !== "closed").length;
    const open = cards.filter(({ stage }) => stage !== "closed").length;
    cells.push({ label: "Needs you", value: waiting, tone: waiting > 0 ? "redline" : "muted" }, { label: "Open campaigns", value: open });
  }
  if (applications.data !== undefined) cells.push({ label: "Applications recorded", value: applications.data.length });

  return (
    <TitleBlock
      title="Applications"
      lines={<p>Your agents research and prepare each application. Nothing leaves without your approval, and you record every change of status yourself.</p>}
      cells={cells}
    />
  );
}
