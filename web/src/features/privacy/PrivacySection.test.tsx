import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authKeys } from "@/features/auth/queries";
import { account, AGREED, aiRoutes, bodyOf, OUTSIDE_COPIES, PROVIDER, subprocessors, VERSION } from "@/test/record";
import { fail, ok, renderWithApi } from "@/test/render";
import { ExportCard } from "./ExportCard";
import { PrivacySection } from "./PrivacySection";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/settings",
  useSearchParams: () => new URLSearchParams("section=privacy"),
  useRouter: () => ({ push: nav.push, replace: nav.replace, refresh: nav.refresh }),
}));

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.refresh.mockReset();
});

const REQUEST = "5a1d3c9e-7b2f-4e08-9c64-0d1e2f3a4b5c";
const exportRequest = (over: Record<string, unknown> = {}) => ({
  id: REQUEST,
  kind: "export",
  status: "queued",
  requested_at: "2026-11-23T05:00:00Z",
  completed_at: null,
  download: null,
  format: "openelip-export-1",
  ...over,
});
const READY = exportRequest({
  status: "succeeded",
  completed_at: "2026-11-23T05:00:09Z",
  download: { method: "GET", path: `/v1/privacy/requests/${REQUEST}/download`, available_until: "2026-11-30T05:00:09Z" },
});

describe("AI processing in settings", () => {
  it("names the providers and the date agreed, and offers Withdraw", async () => {
    renderWithApi(<PrivacySection />, aiRoutes());
    const card = (await screen.findByRole("heading", { level: 2, name: "AI processing" })).closest("section")!;
    expect(await within(card).findByText(/You agreed on/)).toHaveTextContent(/You agreed on 22 Nov( 2026)? 10:00 UTC\. Providers: Example Model Provider\./);
    expect(within(card).getByRole("button", { name: "Withdraw" })).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Agree" })).not.toBeInTheDocument();
  });

  it("withdraws with one write, then says AI is off and offers the sheet again", async () => {
    const user = userEvent.setup();
    let agreed = true;
    const { calls } = renderWithApi(<PrivacySection />, {
      ...aiRoutes(),
      "GET /v1/me": () => ok(account(agreed ? AGREED : {})),
      "PATCH /v1/me": () => {
        agreed = false;
        return ok(account());
      },
    });
    await user.click(await screen.findByRole("button", { name: "Withdraw" }));

    expect(await screen.findByText("AI processing is withdrawn. AI features are off.")).toBeInTheDocument();
    const patch = calls.find((call) => call.method === "PATCH")!;
    expect(await bodyOf(patch)).toEqual({ consents: [{ purpose: "ai_processing", granted: false }] });
    expect(patch.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByRole("button", { name: "Agree" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Withdraw" })).not.toBeInTheDocument();
  });

  it("asks to agree again when the providers changed since the agreement", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<PrivacySection />, {
      ...aiRoutes(),
      "GET /v1/me": ok(account({ granted: true, granted_at: "2026-11-20T09:30:00Z", text_version: "000000000000" })),
      "PATCH /v1/me": ok(account(AGREED)),
    });
    expect(await screen.findByText("Providers changed, agree again")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Agree" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    expect(await bodyOf(calls.find((call) => call.method === "PATCH")!)).toEqual({ consents: [{ purpose: "ai_processing", granted: true, text_version: VERSION }] });
  });

  it("says AI is off until the person agrees, and shows the sheet", async () => {
    renderWithApi(<PrivacySection />, aiRoutes({ agreed: false }));
    expect(await screen.findByText(/You have not agreed to AI processing, so AI features are off/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Agree" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Withdraw" })).not.toBeInTheDocument();
  });

  it("says AI is off on this installation, and only that, when no model is configured", async () => {
    renderWithApi(<PrivacySection />, aiRoutes({ agreed: false, providers: [] }));
    expect(await screen.findByText("AI features are off on this installation")).toBeInTheDocument();
    expect(screen.queryByText("Turn on AI processing to use this")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Agree" })).not.toBeInTheDocument();
  });
});

describe("exporting the data", () => {
  it("asks once, polls the request until the archive is ready, then links to the download and says how long it is kept", async () => {
    const user = userEvent.setup();
    const stages = [exportRequest({ status: "queued" }), exportRequest({ status: "running" }), READY];
    let polled = 0;
    const { calls } = renderWithApi(<ExportCard pollMs={5} />, {
      "POST /v1/privacy/exports": ok(exportRequest(), 202),
      [`GET /v1/privacy/requests/${REQUEST}`]: () => ok(stages[Math.min(polled++, 2)]),
    });
    await user.click(screen.getByRole("button", { name: "Export all data" }));

    expect(await screen.findByText("Preparing your export. This can take a minute.")).toBeInTheDocument();
    const link = await screen.findByRole("link", { name: "Download your data (ZIP)" });
    expect(link).toHaveAttribute("href", `/v1/privacy/requests/${REQUEST}/download`);
    expect(link).toHaveAttribute("download");
    expect(screen.getByText(/Available until/)).toHaveTextContent(/30 Nov( 2026)? 05:00 UTC/);
    expect(screen.getByText(/The file is kept for 7 days/)).toBeInTheDocument();
    const post = calls.find((call) => call.method === "POST")!;
    expect(post.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);

    const before = polled;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(polled).toBe(before); // it stops asking once there is a download
  });

  it("says an export has expired, and offers a new one", async () => {
    const user = userEvent.setup();
    renderWithApi(<ExportCard pollMs={5} />, {
      "POST /v1/privacy/exports": ok(exportRequest(), 202),
      [`GET /v1/privacy/requests/${REQUEST}`]: ok(exportRequest({ status: "succeeded", completed_at: "2026-11-01T05:00:09Z", download: null })),
    });
    await user.click(screen.getByRole("button", { name: "Export all data" }));
    expect(await screen.findByText("This export has expired. Ask for a new one.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export again" })).toBeInTheDocument();
  });

  it("says an export failed and that nothing was lost", async () => {
    const user = userEvent.setup();
    renderWithApi(<ExportCard pollMs={5} />, {
      "POST /v1/privacy/exports": ok(exportRequest(), 202),
      [`GET /v1/privacy/requests/${REQUEST}`]: ok(exportRequest({ status: "failed" })),
    });
    await user.click(screen.getByRole("button", { name: "Export all data" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The export failed. Nothing was lost. Try again.");
  });

  it("says how long to wait when too many exports were asked for", async () => {
    const user = userEvent.setup();
    renderWithApi(<ExportCard />, { "POST /v1/privacy/exports": fail(429, "rate_limited", { retry_after_seconds: 7200 }) });
    await user.click(screen.getByRole("button", { name: "Export all data" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Try again in 2 hours.");
  });
});

describe("deleting the account", () => {
  const routes = (more: Record<string, unknown> = {}) => ({ ...aiRoutes(), "GET /v1/privacy/subprocessors": subprocessors(PROVIDER), ...more });

  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Delete account" }));
    return screen.findByRole("dialog", { name: "Delete your account?" });
  }

  it("lists what is removed and what cannot be reached, and will not delete before a code is sent", async () => {
    const user = userEvent.setup();
    renderWithApi(<PrivacySection />, routes());
    const dialog = await openDialog(user);
    const removed = within(dialog).getAllByRole("listitem").map((item) => item.textContent);
    expect(removed).toEqual([
      "Your record, which holds the rest of this list",
      "Your uploaded documents (resumes)",
      "Your facts, confirmed or not",
      "Your hard constraints",
      "Your preferences",
      "The change log",
      "Your consents",
      "Your sessions: you are signed out everywhere",
    ]);
    expect(within(dialog).getByText(OUTSIDE_COPIES)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Delete my account" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("sends a code to the account's own address, then asks for it, and keeps Delete unavailable until it has six digits", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<PrivacySection />, routes({ "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }, 202) }));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Send code" }));

    expect(await within(dialog).findByText(/We sent a six-digit code to/)).toHaveTextContent("asha@example.com. It is valid for 10 minutes.");
    expect(await bodyOf(calls.find((call) => call.method === "POST")!)).toEqual({ email: "asha@example.com" });
    const confirm = within(dialog).getByRole("button", { name: "Delete my account" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox", { name: "Code from the email" }), "12345");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox", { name: "Code from the email" }), "6");
    expect(confirm).toBeEnabled();
  });

  it("deletes with the code, signs out, forgets the account and goes to the confirmation page", async () => {
    const user = userEvent.setup();
    const { calls, queryClient } = renderWithApi(<PrivacySection />, routes({
      "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }, 202),
      "POST /v1/privacy/deletion": ok({ ...exportRequest({ kind: "deletion" }), revoked: { sessions: 1 } }, 202),
    }));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Send code" }));
    await user.type(await within(dialog).findByRole("textbox", { name: "Code from the email" }), "123 456"); // a pasted code may have a space
    await user.click(within(dialog).getByRole("button", { name: "Delete my account" }));

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/app/settings/deleted"));
    const deletion = calls.find((call) => new URL(call.url).pathname === "/v1/privacy/deletion")!;
    expect(await bodyOf(deletion)).toEqual({ code: "123456" });
    expect(deletion.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(queryClient.getQueryData(authKeys.me)).toBeNull();
    expect(nav.refresh).toHaveBeenCalled();
  });

  it("changes nothing for a wrong code, says how many tries are left, and stays in the dialog", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderWithApi(<PrivacySection />, routes({
      "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }, 202),
      "POST /v1/privacy/deletion": fail(401, "code_invalid", { attempts_remaining: 4 }),
    }));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Send code" }));
    await user.type(await within(dialog).findByRole("textbox", { name: "Code from the email" }), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Delete my account" }));

    expect(await within(dialog).findByText("That code does not match or has expired. Nothing was deleted. 4 tries left.")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
    expect(queryClient.getQueryData<{ email: string }>(authKeys.me)?.email).toBe("asha@example.com");
    expect(screen.getByRole("dialog", { name: "Delete your account?" })).toBeInTheDocument();
  });

  it("says a new code is needed when the tries are used up", async () => {
    const user = userEvent.setup();
    renderWithApi(<PrivacySection />, routes({
      "POST /v1/auth/codes": ok({ expires_in_seconds: 600 }, 202),
      "POST /v1/privacy/deletion": fail(401, "code_invalid", { attempts_remaining: 0 }),
    }));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Send code" }));
    await user.type(await within(dialog).findByRole("textbox", { name: "Code from the email" }), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Delete my account" }));
    expect(await within(dialog).findByText("That code can no longer be used. Send a new one.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Send a new code" })).toBeInTheDocument();
  });

  it("closes on Cancel and sends nothing but reads", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<PrivacySection />, routes());
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.filter((call) => call.method !== "GET")).toHaveLength(0);
  });
});

describe("the settings page", () => {
  it("sends a signed-out person to sign in and back to the privacy settings", async () => {
    renderWithApi(<PrivacySection />, { "GET /v1/me": fail(401, "unauthenticated"), "GET /v1/privacy/subprocessors": subprocessors(PROVIDER) });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/sign-in?next=%2Fapp%2Fsettings%3Fsection%3Dprivacy"));
  });
});
