import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { authKeys } from "@/features/auth/queries";
import { fail, ok, renderWithApi } from "@/test/render";
import { AiConsentSheet } from "./AiConsentSheet";

// Invented providers and an invented account (example.com is reserved for examples)
const PROVIDER = {
  name: "Example Model Provider",
  kind: "model_provider",
  purpose: "Reads resume lines and returns the facts they state.",
  receives: "Resume lines and chat messages, which can identify a person. Retention as stated by the provider.",
  tasks: ["resume.facts.roles"],
};
const OTHER_PROVIDER = { ...PROVIDER, name: "Second Example Provider", receives: "Candidate facts with name and email removed." };
const EMAIL = { name: "mail.example.test", kind: "email", purpose: "Delivers sign-in codes.", receives: "Email addresses.", tasks: [] };
const list = (...items: unknown[]) => ok({ items, outside_copies_statement: "Copies made outside this system cannot be reached by deletion." });

function account(ai: Record<string, unknown> = {}) {
  return {
    id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780",
    email: "asha@example.com",
    timezone: "UTC",
    created_at: "2026-11-22T00:00:00Z",
    consents: [
      { purpose: "terms", granted: false, granted_at: null, text_version: null },
      {
        purpose: "ai_processing",
        granted: false,
        granted_at: null,
        text_version: null,
        providers: ["Example Model Provider"],
        current_text_version: "a1b2c3d4e5f6",
        ...ai,
      },
      { purpose: "brief_email", granted: false, granted_at: null, text_version: null },
      { purpose: "open_to_introductions", granted: false, granted_at: null, text_version: null },
    ],
  };
}

const bodyOf = (request: Request) => request.clone().json();

describe("the AI processing consent sheet", () => {
  it("lists each model provider with what it receives and why, and leaves out the email host", async () => {
    renderWithApi(<AiConsentSheet />, {
      "GET /v1/me": ok(account()),
      "GET /v1/privacy/subprocessors": list(PROVIDER, OTHER_PROVIDER, EMAIL),
    });
    const providers = await screen.findByRole("list", { name: "AI model providers" });
    const rows = within(providers).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Example Model Provider");
    expect(rows[0]).toHaveTextContent("Resume lines and chat messages, which can identify a person.");
    expect(rows[0]).toHaveTextContent("Reads resume lines and returns the facts they state.");
    expect(rows[1]).toHaveTextContent("Second Example Provider");
    expect(screen.queryByText("mail.example.test")).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent until you agree/)).toBeInTheDocument();
  });

  it("agrees with the wording version it displayed, then tells the caller and keeps the new account", async () => {
    const user = userEvent.setup();
    const onAgreed = vi.fn();
    const agreed = account({ granted: true, granted_at: "2026-11-23T05:00:00Z", text_version: "a1b2c3d4e5f6" });
    const { calls, queryClient } = renderWithApi(<AiConsentSheet onAgreed={onAgreed} />, {
      "GET /v1/me": ok(account()),
      "GET /v1/privacy/subprocessors": list(PROVIDER, EMAIL),
      "PATCH /v1/me": ok(agreed),
    });
    await user.click(await screen.findByRole("button", { name: "Agree" }));

    await waitFor(() => expect(onAgreed).toHaveBeenCalledTimes(1));
    const patch = calls.find((call) => call.method === "PATCH")!;
    expect(await bodyOf(patch)).toEqual({ consents: [{ purpose: "ai_processing", granted: true, text_version: "a1b2c3d4e5f6" }] });
    expect(patch.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(queryClient.getQueryData<typeof agreed>(authKeys.me)?.consents[1].granted).toBe(true);
  });

  it("reloads the providers and asks again when the wording changed while the sheet was open", async () => {
    const user = userEvent.setup();
    const onAgreed = vi.fn();
    let switched = false; // the installation moves to a second provider just as the first Agree arrives
    const sent: unknown[] = [];
    renderWithApi(<AiConsentSheet onAgreed={onAgreed} />, {
      "GET /v1/me": () => ok(account(switched ? { current_text_version: "ffffffffffff" } : {})),
      "GET /v1/privacy/subprocessors": () => (switched ? list(OTHER_PROVIDER) : list(PROVIDER)),
      "PATCH /v1/me": async (request) => {
        sent.push(await bodyOf(request));
        if (sent.length > 1) return ok(account({ granted: true }));
        switched = true;
        return fail(409, "conflict", { state: "consent_text_changed", current_version: "ffffffffffff" });
      },
    });
    await user.click(await screen.findByRole("button", { name: "Agree" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The list of providers changed while you were reading. Nothing was recorded.");
    expect(await screen.findByText("Second Example Provider")).toBeInTheDocument();
    expect(onAgreed).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Agree" }));
    await waitFor(() => expect(onAgreed).toHaveBeenCalledTimes(1));
    expect(sent).toEqual([
      { consents: [{ purpose: "ai_processing", granted: true, text_version: "a1b2c3d4e5f6" }] },
      { consents: [{ purpose: "ai_processing", granted: true, text_version: "ffffffffffff" }] },
    ]);
  });

  it("offers Not now only when the caller can continue without AI, and sends nothing for it", async () => {
    const user = userEvent.setup();
    const onNotNow = vi.fn();
    const routes = { "GET /v1/me": ok(account()), "GET /v1/privacy/subprocessors": list(PROVIDER) };
    const first = renderWithApi(<AiConsentSheet />, routes);
    await screen.findByRole("button", { name: "Agree" });
    expect(screen.queryByRole("button", { name: "Not now" })).not.toBeInTheDocument();
    first.unmount();

    const { calls } = renderWithApi(<AiConsentSheet onNotNow={onNotNow} />, routes);
    await user.click(await screen.findByRole("button", { name: "Not now" }));
    expect(onNotNow).toHaveBeenCalledTimes(1);
    expect(calls.filter((call) => call.method !== "GET")).toHaveLength(0);
  });

  it("says the providers changed, and since when, for an agreement given to a different list", async () => {
    renderWithApi(<AiConsentSheet />, {
      "GET /v1/me": ok(account({ granted: true, granted_at: "2026-11-20T09:30:00Z", text_version: "000000000000" })),
      "GET /v1/privacy/subprocessors": list(PROVIDER),
    });
    const sentence = await screen.findByText(/You agreed to AI processing on/);
    // the year is shown only when it is not the current one, so the pattern allows it
    expect(sentence).toHaveTextContent(/You agreed to AI processing on 20 Nov( 2026)? 09:30 UTC, for a different list of providers\./);
    expect(sentence.querySelector("time")).toHaveAttribute("datetime", "2026-11-20T09:30:00Z");
    expect(screen.getByRole("button", { name: "Agree" })).toBeInTheDocument();
  });

  it("shows what failed in plain words and records nothing when the save fails for another reason", async () => {
    const user = userEvent.setup();
    const onAgreed = vi.fn();
    renderWithApi(<AiConsentSheet onAgreed={onAgreed} />, {
      "GET /v1/me": ok(account()),
      "GET /v1/privacy/subprocessors": list(PROVIDER),
      "PATCH /v1/me": fail(500, "internal_error"),
    });
    await user.click(await screen.findByRole("button", { name: "Agree" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on the server. Nothing was sent.");
    expect(onAgreed).not.toHaveBeenCalled();
  });
});
