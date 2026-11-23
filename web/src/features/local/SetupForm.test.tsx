import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { SetupForm } from "./SetupForm";

// One router object for the whole file: the real useRouter returns a stable one, and an effect depends on it
const nav = vi.hoisted(() => {
  const router = { replace: vi.fn(), refresh: vi.fn(), push: vi.fn() };
  return { ...router, router };
});
vi.mock("next/navigation", () => ({ useRouter: () => nav.router }));

const status = (model: Record<string, unknown>) => ok({ mode: "local", has_owner: true, model });
const none = { provider: "anthropic", key_set: false, key_last4: null, source: null };
const KEY = "sk-ant-api03-an-invented-key-for-the-test";

beforeEach(() => {
  nav.replace.mockReset();
  nav.refresh.mockReset();
});

describe("the setup page", () => {
  it("asks for the provider and the key, with a visible label on each", async () => {
    renderWithApi(<SetupForm next="/jobs" />, { "GET /v1/local/status": status(none) });
    expect(await screen.findByLabelText("API key")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Provider")).toHaveValue("anthropic");
    expect(screen.getByRole("link", { name: "Skip for now" })).toHaveAttribute("href", "/jobs");
  });

  it("offers the demo data only when the stack runs the fake provider", async () => {
    renderWithApi(<SetupForm next="/trust" />, { "GET /v1/local/status": status({ ...none, provider: "fake" }) });
    const skip = await screen.findByRole("link", { name: "Skip for now, explore with demo data" });
    expect(skip).toHaveAttribute("href", "/trust");
  });

  it("sends the provider and the key as JSON, then leaves for where the person was going", async () => {
    const user = userEvent.setup();
    const saved = { provider: "openai", key_set: true, key_last4: "test", source: "app" };
    const { calls } = renderWithApi(<SetupForm next="/jobs" />, {
      "GET /v1/local/status": status(none),
      "PUT /v1/settings/model-key": ok(saved),
    });
    await user.selectOptions(await screen.findByLabelText("Provider"), "openai");
    await user.type(screen.getByLabelText("API key"), `  ${KEY}  `);
    await user.click(screen.getByRole("button", { name: "Save key" }));

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/jobs"));
    const put = calls.find((call) => call.method === "PUT")!;
    expect(await put.clone().json()).toEqual({ provider: "openai", api_key: KEY });
    expect(put.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("says the key is not valid in words, without echoing it, and stays on the page", async () => {
    const user = userEvent.setup();
    renderWithApi(<SetupForm next="/jobs" />, {
      "GET /v1/local/status": status(none),
      "PUT /v1/settings/model-key": fail(422, "validation_failed", { fields: [{ field: "api_key", issue: "x" }] }),
    });
    await user.type(await screen.findByLabelText("API key"), "short");
    await user.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByText(/does not look like an API key/)).toBeInTheDocument();
    expect(screen.queryByText("short")).not.toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("says when .env already settles the model", async () => {
    const user = userEvent.setup();
    renderWithApi(<SetupForm next="/jobs" />, {
      "GET /v1/local/status": status(none),
      "PUT /v1/settings/model-key": fail(409, "conflict", { state: "set_in_environment" }),
    });
    await user.type(await screen.findByLabelText("API key"), KEY);
    await user.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByText(/The model is set in your \.env file/)).toBeInTheDocument();
  });

  it("shows only the last four characters of a key that is already in use", async () => {
    renderWithApi(<SetupForm next="/jobs" />, {
      "GET /v1/local/status": status({ provider: "anthropic", key_set: true, key_last4: "LAST", source: "app" }),
    });
    await screen.findByLabelText("API key");
    expect(screen.getByRole("status")).toHaveTextContent("A key ending in LAST is in use (saved here).");
  });

  it("does not pretend setup exists on a server that is not in local mode", async () => {
    renderWithApi(<SetupForm next="/jobs" />, { "GET /v1/local/status": fail(404, "not_found") });
    expect(await screen.findByRole("alert")).toHaveTextContent("APP_MODE=local");
  });
});
