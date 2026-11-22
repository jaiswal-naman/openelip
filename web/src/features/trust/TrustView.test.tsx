import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { fail, ok, renderWithApi } from "@/test/render";
import { TrustView } from "./TrustView";

// Invented providers (example.test addresses are not real services)
const PROVIDER = {
  name: "Example Model Provider",
  kind: "model_provider",
  purpose: "Checks that the model connection works.",
  receives: "Invented test messages only. Retention as stated by the provider.",
  tasks: ["ping"],
};
const EMAIL = {
  name: "mail.example.test",
  kind: "email",
  purpose: "Delivers the sign-in codes this installation emails.",
  receives: "Email addresses and the sign-in codes sent to them.",
  tasks: [],
};
const STATEMENT = "Copies made outside this system, for example an email that has already been delivered to someone else, cannot be reached by deletion.";

const list = (items: unknown[]) => ok({ items, outside_copies_statement: STATEMENT });

describe("the trust page", () => {
  it("lists every provider and sub-processor the API names, with purpose and what is sent", async () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": list([PROVIDER, EMAIL]) });
    const table = await screen.findByRole("table", { name: /Providers and sub-processors/ });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Name", "Purpose", "Data sent"]);

    const provider = within(table).getByRole("row", { name: /Example Model Provider/ });
    expect(provider).toHaveTextContent("Model provider");
    expect(provider).toHaveTextContent("Checks that the model connection works.");
    expect(provider).toHaveTextContent("Invented test messages only.");
    expect(provider).toHaveTextContent("Tasks: ping");

    const email = within(table).getByRole("row", { name: /mail\.example\.test/ });
    expect(email).toHaveTextContent("Email host");
    expect(email).not.toHaveTextContent("Tasks:");
  });

  it("states that copies made outside the system cannot be reached by deletion", async () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": list([PROVIDER, EMAIL]) });
    expect(await screen.findByText(STATEMENT)).toBeInTheDocument();
  });

  it("does not say AI is off when a model provider is in use", async () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": list([PROVIDER, EMAIL]) });
    await screen.findByRole("table");
    expect(screen.queryByText("AI features are off on this server")).not.toBeInTheDocument();
  });

  it("says AI is off when no model provider is configured, and still lists the email host", async () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": list([EMAIL]) });
    expect(await screen.findByText("AI features are off on this server")).toBeInTheDocument();
    expect(screen.getByText(/No model provider is configured on this server/)).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /mail\.example\.test/ })).toBeInTheDocument();
  });

  it("shows a kind it does not know as it came, instead of dropping the row", async () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": list([{ ...EMAIL, name: "Future Service", kind: "storage" }]) });
    expect(await screen.findByRole("row", { name: /Future Service/ })).toHaveTextContent("storage");
  });

  it("shows a loading state shaped like the table while the list loads", () => {
    renderWithApi(<TrustView />, { "GET /v1/privacy/subprocessors": () => new Promise<Response>(() => {}) });
    expect(screen.getByRole("status", { name: "Loading the list of providers" })).toBeInTheDocument();
  });

  it("shows what failed with a Retry that works", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    renderWithApi(<TrustView />, {
      "GET /v1/privacy/subprocessors": () => (++attempt === 1 ? fail(503, "dependency_unavailable") : list([EMAIL])),
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the list of providers.");
    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});
