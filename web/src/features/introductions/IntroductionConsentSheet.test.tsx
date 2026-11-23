import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { IntroductionConsentSheet } from "./IntroductionConsentSheet";
import { ASK, detail, signals } from "./testing";

const GET = `GET /v1/introductions/${ASK}`;
const ACCEPT = `POST /v1/introductions/${ASK}/accept`;
const DECLINE = `POST /v1/introductions/${ASK}/decline`;

function open(routes: Record<string, Response | ((request: Request) => Response | Promise<Response>)> = {}) {
  const onClose = vi.fn();
  const onDone = vi.fn();
  const rendered = renderWithApi(<IntroductionConsentSheet introductionId={ASK} isOpen onClose={onClose} onDone={onDone} />, {
    [GET]: ok(detail()),
    "GET /v1/signals": ok(signals()),
    ...routes,
  });
  return { ...rendered, onClose, onDone, user: userEvent.setup() };
}

const posted = (calls: Request[], key: string) => calls.filter((request) => `${request.method} ${new URL(request.url).pathname}` === key);

describe("the introduction consent sheet", () => {
  it("moves focus to its title and shows exactly the server's preview: name, headline, evidence with quotes, facts, email rule, pay and terms", async () => {
    open();
    const title = await screen.findByRole("heading", { name: "Share with Halden Demo for Backend engineer?" });
    await waitFor(() => expect(title).toHaveFocus());
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Demo Candidate")).toBeInTheDocument();
    expect(within(dialog).getAllByText(/Backend engineer at Initech Demo, 2018 to 2024\./).length).toBeGreaterThan(0);
    const table = within(dialog).getByRole("grid", { name: "Requirements and your evidence that would be shared" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]!).getByText("Met")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("“Skills: Python, SQL, Quillscript.”")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Partly met")).toBeInTheDocument();
    expect(within(dialog).getByText("BSc Computer Science, Example University Demo, 2018.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Your email is shared only if they answer yes: then they receive candidate@example\.test/)).toBeInTheDocument();
    expect(within(dialog).getByText("EUR 70,000 to 95,000 a year")).toBeInTheDocument();
    expect(within(dialog).getByText(/must answer yes or no with a reason within 5 business days/)).toBeInTheDocument();
    expect(within(dialog).getByText("Files, current pay, other contact details.")).toBeInTheDocument();
  });

  it("starts with nothing ticked: the serious-interest box is off and shows the quota", async () => {
    open();
    const box = await screen.findByRole("checkbox", { name: "Send serious interest (2 of 3 left this week)" });
    expect(box).not.toBeChecked();
    expect(screen.getAllByRole("checkbox").filter((element) => (element as HTMLInputElement).checked)).toHaveLength(0);
  });

  it("accepts with the hash and wording version from the GET, sends no signal unasked, and closes", async () => {
    const { user, calls, onDone } = open({ [ACCEPT]: ok({ introduction: { id: ASK, status: "accepted" }, shared: "x" }) });
    await user.click(await screen.findByRole("button", { name: "Accept and share" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("accepted"));
    const [request] = posted(calls, ACCEPT);
    expect(await request!.clone().json()).toEqual({ share_preview_hash: "hash-one", consent_text_version: "introduction-2026-11" });
    expect(request!.headers.get("Idempotency-Key")).toBeTruthy();
    expect(posted(calls, "POST /v1/signals")).toHaveLength(0);
  });

  it("sends one serious-interest signal after the accept when the person ticked it", async () => {
    const { user, calls, onDone } = open({
      [ACCEPT]: ok({ introduction: { id: ASK, status: "accepted" }, shared: "x" }),
      "POST /v1/signals": ok({ id: "s1" }, 201),
    });
    await user.click(await screen.findByRole("checkbox", { name: /Send serious interest/ }));
    await user.click(screen.getByRole("button", { name: "Accept and share" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("accepted"));
    const sent = posted(calls, "POST /v1/signals");
    expect(sent).toHaveLength(1);
    expect(await sent[0]!.clone().json()).toEqual({ introduction_id: ASK });
    expect(calls.indexOf(posted(calls, ACCEPT)[0]!)).toBeLessThan(calls.indexOf(sent[0]!));
  });

  it("on 409 preview_changed says so, reloads the new content, accepts nothing, and the next accept sends the new hash", async () => {
    let version = 1;
    const accepts: unknown[] = [];
    const { user, onDone } = open({
      [GET]: () => ok(version === 1 ? detail() : detail({ share_preview_hash: "hash-two", share_preview: { ...detail().share_preview, on_accept: { ...detail().share_preview.on_accept, headline: "Staff engineer at Initech Demo" } } })),
      [ACCEPT]: async (request) => {
        accepts.push(await request.json());
        if (version === 1) {
          version = 2;
          return fail(409, "preview_changed");
        }
        return ok({ introduction: { id: ASK, status: "accepted" }, shared: "x" });
      },
    });
    await user.click(await screen.findByRole("checkbox", { name: /Send serious interest/ }));
    await user.click(screen.getByRole("button", { name: "Accept and share" }));
    expect(await screen.findByText("What would be shared changed. Review it again.")).toBeInTheDocument();
    expect(await screen.findByText(/Staff engineer at Initech Demo/)).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: /Send serious interest/ })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Accept and share" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("accepted"));
    expect(accepts).toEqual([
      { share_preview_hash: "hash-one", consent_text_version: "introduction-2026-11" },
      { share_preview_hash: "hash-two", consent_text_version: "introduction-2026-11" },
    ]);
  });

  it.each([
    [fail(409, "conflict", { state: "expired" }), "This ask has expired, so it can no longer be accepted."],
    [fail(409, "already_resolved", { resolution: "accepted" }), "You already answered this ask."],
  ])("on a refusal that ends the ask, says why and offers only Close", async (response, words) => {
    const { user, onClose, onDone } = open({ [ACCEPT]: response, "GET /v1/introductions": ok([]) });
    await user.click(await screen.findByRole("button", { name: "Accept and share" }));
    expect(await screen.findByText(words)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept and share" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("Not now closes and sends nothing", async () => {
    const { user, calls, onClose } = open();
    await user.click(await screen.findByRole("button", { name: "Not now" }));
    expect(onClose).toHaveBeenCalled();
    expect(calls.filter((request) => request.method !== "GET")).toHaveLength(0);
  });

  it("Decline asks first, then declines", async () => {
    const { user, calls, onDone } = open({ [DECLINE]: ok({ introduction: { id: ASK, status: "declined" } }) });
    await user.click(await screen.findByRole("button", { name: "Decline" }));
    expect(posted(calls, DECLINE)).toHaveLength(0);
    const confirm = screen.getByRole("group", { name: "Confirm decline" });
    expect(within(confirm).getByText(/The employer is not told/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Keep it open" }));
    expect(screen.getByRole("button", { name: "Accept and share" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Decline" }));
    await user.click(screen.getByRole("button", { name: "Yes, decline" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("declined"));
    expect(posted(calls, DECLINE)).toHaveLength(1);
  });

  it("renders organization and answer text as text, never as markup", async () => {
    open({ [GET]: ok(detail({ organization: { id: "o", name: "<b>Bold Demo</b>", is_synthetic: true } })) });
    expect(await screen.findByRole("heading", { name: "Share with <b>Bold Demo</b> for Backend engineer?" })).toBeInTheDocument();
  });
});
