import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BriefIntroductions, introductionItems } from "./BriefIntroductions";

const ASK = "7a1b2c3d-0000-4000-8000-0000000000a1";
const intro = { kind: "introduction" as const, introduction_id: ASK, organization_name: "Halden Demo", role_title: "Backend engineer", expires_at: "2026-11-30T09:00:00Z" };

describe("introduction items of a brief", () => {
  it("renders each as a line with organization, role, expiry and a link that opens its consent sheet", () => {
    render(<BriefIntroductions items={[intro]} />);
    const section = screen.getByRole("region", { name: "Introductions in this brief" });
    expect(within(section).getByText("Halden Demo asks about Backend engineer. Answer by 30 Nov.")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Review and accept" })).toHaveAttribute("href", `/app/today?decision=${ASK}`);
  });

  it("picks the introduction items out of a brief's decisions and renders nothing without any", () => {
    const match = { kind: "match" as const, title: "t", why: "w", ref: {}, actions: [], is_assessed: true, change_note: null, note: null };
    expect(introductionItems([match, intro])).toEqual([intro]);
    const { container } = render(<BriefIntroductions items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
