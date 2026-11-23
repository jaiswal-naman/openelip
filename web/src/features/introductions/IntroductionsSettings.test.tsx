import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { account } from "@/test/record";
import { ok, renderWithApi } from "@/test/render";
import { OPEN_TO_INTRODUCTIONS_TEXT_VERSION } from "./api";
import { IntroductionsSettings } from "./IntroductionsSettings";

const consent = (granted: boolean) => ({ purpose: "open_to_introductions", granted, granted_at: granted ? "2026-11-20T00:00:00Z" : null, text_version: granted ? OPEN_TO_INTRODUCTIONS_TEXT_VERSION : null });

function setup(granted: boolean) {
  let current = granted;
  const me = () => account({}, { consents: [consent(current)] });
  const rendered = renderWithApi(<IntroductionsSettings />, {
    "GET /v1/me": () => ok(me()),
    "PATCH /v1/me": async (request) => {
      const body = (await request.clone().json()) as { consents: { granted: boolean }[] };
      current = body.consents[0]!.granted;
      return ok(me());
    },
  });
  return { ...rendered, user: userEvent.setup() };
}

const patches = (calls: Request[]) => calls.filter((request) => request.method === "PATCH");

describe("the Introductions section of settings", () => {
  it("turning the switch on sends the purpose, granted true and the wording version", async () => {
    const { user, calls } = setup(false);
    const toggle = await screen.findByRole("switch", { name: "Open to introductions from employers" });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText("Off. Asks you have already received stay open, and no new ones arrive.")).toBeInTheDocument();
    await user.click(toggle);
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(await patches(calls)[0]!.clone().json()).toEqual({
      consents: [{ purpose: "open_to_introductions", granted: true, text_version: OPEN_TO_INTRODUCTIONS_TEXT_VERSION }],
    });
    await waitFor(() => expect(screen.getByRole("switch", { name: "Open to introductions from employers" })).toBeChecked());
  });

  it("turning it off withdraws with granted false only, and says open asks stay but no new ones arrive", async () => {
    const { user, calls } = setup(true);
    const toggle = await screen.findByRole("switch", { name: "Open to introductions from employers" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(await patches(calls)[0]!.clone().json()).toEqual({ consents: [{ purpose: "open_to_introductions", granted: false }] });
    expect(await screen.findByText("Off. Asks you have already received stay open, and no new ones arrive.")).toBeInTheDocument();
  });

  it("says where employers are blocked: the constraints of the record", async () => {
    setup(true);
    expect(await screen.findByText(/Employers you block never see you\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Block employers in your constraints" })).toHaveAttribute("href", "/app/record");
  });
});
