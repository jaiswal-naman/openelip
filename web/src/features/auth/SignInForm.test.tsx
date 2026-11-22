import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { authKeys } from "./queries";
import { SignInForm } from "./SignInForm";

const nav = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: nav.replace, refresh: nav.refresh, push: vi.fn() }) }));

// An invented account
const me = { id: "5c9d1e3f-4a6b-4c8d-8e2f-a3b4c5d6e780", email: "reader@example.test", timezone: "UTC", created_at: "2026-11-22T00:00:00Z" };
const codeSent = () => ok({ expires_in_seconds: 600 }, 202);

const body = async (request: Request) => JSON.parse(await request.clone().text());

beforeEach(() => {
  nav.replace.mockReset();
  nav.refresh.mockReset();
});
afterEach(() => vi.useRealTimers());

async function toStepTwo(user: ReturnType<typeof userEvent.setup>, email = "reader@example.test") {
  await user.type(screen.getByRole("textbox", { name: "Email" }), email);
  await user.click(screen.getByRole("button", { name: "Send code" }));
  await screen.findByRole("textbox", { name: "Code" });
}

describe("step 1: the email", () => {
  it("asks for the email with a visible label and nothing else", () => {
    renderWithApi(<SignInForm next="/jobs" />);
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveAttribute("type", "email");
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveAttribute("autocomplete", "email");
    expect(screen.queryByRole("textbox", { name: "Code" })).not.toBeInTheDocument();
  });

  it("posts the address as JSON with an Idempotency-Key, and moves on to step 2", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await toStepTwo(user);

    expect(calls).toHaveLength(1);
    expect(await body(calls[0])).toEqual({ email: "reader@example.test" });
    expect(calls[0].headers.get("Content-Type")).toBe("application/json");
    expect(calls[0].headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(screen.getByText(/Code sent to/)).toHaveTextContent("Code sent to reader@example.test. It is valid for 10 minutes.");
  });

  it("says the same thing for every address, so the page never reveals whether an account exists", async () => {
    const wording = async (email: string) => {
      const user = userEvent.setup();
      const { unmount } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
      await toStepTwo(user, email);
      const text = screen.getByText(/Code sent to/).textContent!.replace(email, "<address>");
      unmount();
      return text;
    };
    expect(await wording("someone.with.an.account@example.test")).toBe(await wording("nobody.has.used.this@example.test"));
  });

  it("shows when to try again after a rate limit, and keeps the address", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": fail(429, "rate_limited", { retry_after_seconds: 840 }, { "Retry-After": "840" }) });
    await user.type(screen.getByRole("textbox", { name: "Email" }), "reader@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));

    expect(await screen.findByText("Too many attempts. Try again in 14 minutes.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveValue("reader@example.test");
    expect(screen.queryByRole("textbox", { name: "Code" })).not.toBeInTheDocument();
  });

  it("explains an address the server refuses, next to the field, and puts the cursor back in it", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": fail(422, "validation_failed", { fields: [{ field: "body.email", issue: "x" }] }) });
    await user.type(screen.getByRole("textbox", { name: "Email" }), "a@b");
    await user.click(screen.getByRole("button", { name: "Send code" }));

    const message = await screen.findByText("Enter a valid email address.");
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveAccessibleDescription(message.textContent!);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Email" })).toHaveFocus());
  });

  it("does not send a request for an empty address, and says so next to the field", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText("Enter your email address.")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("can simply be tried again after an error, without editing anything", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    renderWithApi(<SignInForm next="/jobs" />, {
      "POST /v1/auth/codes": () => (++attempt === 1 ? Promise.reject(new TypeError("Failed to fetch")) : codeSent()),
    });
    await user.type(screen.getByRole("textbox", { name: "Email" }), "reader@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByRole("textbox", { name: "Code" })).toBeInTheDocument();
  });

  it("names a network failure plainly", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": () => Promise.reject(new TypeError("Failed to fetch")) });
    await user.type(screen.getByRole("textbox", { name: "Email" }), "reader@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();
  });
});

describe("step 2: the code", () => {
  it("takes a six-digit code the way phones offer it, and allows pasting", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await toStepTwo(user);
    const box = screen.getByRole("textbox", { name: "Code" });
    expect(box).toHaveAttribute("autocomplete", "one-time-code");
    expect(box).toHaveAttribute("inputmode", "numeric");
    expect(box).toHaveAttribute("type", "text"); // a number box would drop a leading zero
    expect(box).toHaveFocus();
    await user.paste("012 345");
    expect(box).toHaveValue("012 345");
  });

  it("signs in with the email, the code and the browser's time zone, then goes to the page you came from", async () => {
    const user = userEvent.setup();
    const { calls, queryClient } = renderWithApi(<SignInForm next="/control/costs" />, {
      "POST /v1/auth/codes": codeSent(),
      "POST /v1/auth/sessions": ok(me, 201),
    });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "012345");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/control/costs"));
    expect(nav.refresh).toHaveBeenCalled();
    expect(await body(calls[1])).toEqual({ email: "reader@example.test", code: "012345", timezone: "UTC" });
    expect(calls[1].headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(queryClient.getQueryData(authKeys.me)).toEqual(me); // the header shows the account without another request
  });

  it("accepts a pasted code with a space in it", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent(), "POST /v1/auth/sessions": ok(me, 201) });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "123 456{Enter}"); // Enter presses Sign in
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/jobs"));
    expect((await body(calls[1])).code).toBe("123456");
  });

  it("shows the plain message for a wrong code, how many tries are left, and keeps the form", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, {
      "POST /v1/auth/codes": codeSent(),
      "POST /v1/auth/sessions": fail(401, "code_invalid", { attempts_remaining: 3 }),
    });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "111111");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/That code does not match or has expired\. Check it, or send a new one\. 3 tries left\./)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveValue("111111"); // the form is kept
    expect(screen.getByText(/Code sent to/)).toHaveTextContent("reader@example.test");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Code" })).toHaveFocus());
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("keeps the error on screen while you correct the code and when the field loses focus", async () => {
    // Pressing Sign in blurs the field first. If the message vanished then, the button would jump up under the pointer and
    // the click would land on the form: the person would have to press twice. Found in a real browser, not in jsdom.
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, {
      "POST /v1/auth/codes": codeSent(),
      "POST /v1/auth/sessions": fail(401, "code_invalid", { attempts_remaining: 3 }),
    });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "111111");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const message = await screen.findByText(/That code does not match/);

    await user.clear(screen.getByRole("textbox", { name: "Code" }));
    await user.type(screen.getByRole("textbox", { name: "Code" }), "222222");
    await user.tab(); // the field loses focus, as it does when the button is pressed
    expect(message).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveAccessibleDescription(/That code does not match/);
  });

  it("says one try is left in the singular, and says to send a new code when none is", async () => {
    const user = userEvent.setup();
    let left = 1;
    renderWithApi(<SignInForm next="/jobs" />, {
      "POST /v1/auth/codes": codeSent(),
      "POST /v1/auth/sessions": () => fail(401, "code_invalid", { attempts_remaining: left-- }),
    });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "111111");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/1 try left\./)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("That code can no longer be used. Send a new one.")).toBeInTheDocument();
  });

  it("does not spend an attempt on something that is not six digits", async () => {
    const user = userEvent.setup();
    const { calls } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await toStepTwo(user);
    await user.type(screen.getByRole("textbox", { name: "Code" }), "12345");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter the six digits from the email.")).toBeInTheDocument();
    expect(calls).toHaveLength(1); // only the code request
  });

  it("lets you go back and use a different email, with the first one filled in", async () => {
    const user = userEvent.setup();
    renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await toStepTwo(user);
    await user.click(screen.getByRole("button", { name: "Use a different email" }));
    expect(await screen.findByRole("textbox", { name: "Email" })).toHaveValue("reader@example.test");
  });
});

describe("resending the code", () => {
  it("waits out a cooldown, counted down in view, then sends a new code", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    const { calls } = renderWithApi(<SignInForm next="/jobs" />, { "POST /v1/auth/codes": codeSent() });
    await toStepTwo(user);

    const resend = await screen.findByRole("button", { name: /^Resend in 0:4\d$/ });
    expect(resend).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(46_000);
    });
    const ready = await screen.findByRole("button", { name: "Send a new code" });
    expect(ready).toBeEnabled();

    await user.click(ready);
    await waitFor(() => expect(calls.filter((call) => call.url.endsWith("/v1/auth/codes"))).toHaveLength(2));
    expect(await screen.findByRole("button", { name: /^Resend in 0:4\d$/ })).toBeDisabled(); // and the cooldown starts again
  });
});
