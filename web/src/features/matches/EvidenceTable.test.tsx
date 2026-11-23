import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { EvidenceTable } from "./EvidenceTable";
import type { EvidenceItem } from "./queries";
import { evidence } from "./testing";

/** A parent that keeps the selection, as MatchDetail does, and shows it so the test can read it. */
function Harness({ items = evidence(), initial = null }: { items?: EvidenceItem[]; initial?: number | null }) {
  const [selected, setSelected] = useState<number | null>(initial);
  return (
    <>
      <EvidenceTable evidence={items} selected={selected} onSelect={setSelected} />
      <p data-testid="selected">{String(selected)}</p>
    </>
  );
}

const rowOf = (name: RegExp) => screen.getByRole("row", { name });

describe("EvidenceTable", () => {
  it("lists every requirement with its importance, its verdict in words and the record's own words for it", () => {
    render(<Harness />);
    const table = screen.getByRole("grid", { name: "Requirements and your evidence" });
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Requirement", "Verdict", "Your evidence"]);
    expect(within(table).getAllByRole("row")).toHaveLength(5); // the header row and four requirements

    const first = rowOf(/3\+ years of backend experience/);
    expect(first).toHaveTextContent("Required");
    expect(first).toHaveTextContent("Met");
    expect(first).toHaveTextContent("“Backend engineer at Initech Demo, 2019 to 2024.”");
    expect(first).toHaveTextContent("From your resume");

    const preferred = rowOf(/Kubernetes in production/);
    expect(preferred).toHaveTextContent("Preferred");
    expect(preferred).toHaveTextContent("No evidence");
    expect(preferred).toHaveTextContent("No evidence in your record");

    expect(rowOf(/lead a small team/)).toHaveTextContent("Partly met");
  });

  it("selects a row when it is clicked, and says which row is selected", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(rowOf(/shipped services in Quillscript/));
    expect(screen.getByTestId("selected")).toHaveTextContent("12");
    expect(rowOf(/shipped services in Quillscript/)).toHaveAttribute("aria-selected", "true");
    expect(rowOf(/3\+ years of backend experience/)).toHaveAttribute("aria-selected", "false");
  });

  it("selects the focused row with Enter and with Space, and moves between rows with the arrow keys", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    rowOf(/3\+ years of backend experience/).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("selected")).toHaveTextContent("11");

    await user.keyboard("{ArrowDown}");
    expect(rowOf(/shipped services in Quillscript/)).toHaveFocus();
    expect(screen.getByTestId("selected")).toHaveTextContent("11"); // moving is not selecting
    await user.keyboard(" ");
    expect(screen.getByTestId("selected")).toHaveTextContent("12");
  });

  it("clears the selection when the selected row is pressed again", async () => {
    const user = userEvent.setup();
    render(<Harness initial={13} />);
    expect(rowOf(/Kubernetes in production/)).toHaveAttribute("aria-selected", "true");
    await user.click(rowOf(/Kubernetes in production/));
    expect(screen.getByTestId("selected")).toHaveTextContent("null");
    expect(rowOf(/Kubernetes in production/)).toHaveAttribute("aria-selected", "false");
  });

  it("shows only one selected row at a time", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(rowOf(/3\+ years of backend experience/));
    await user.click(rowOf(/lead a small team/));
    expect(screen.getByTestId("selected")).toHaveTextContent("14");
    expect(screen.getAllByRole("row").filter((row) => row.getAttribute("aria-selected") === "true")).toHaveLength(1);
  });

  it("says where each fact came from: a resume, or the person's own words, and whether it has changed since", () => {
    const items = evidence();
    items[0].record_citations = [{ ...items[0].record_citations[0], source: null }];
    items[1].record_citations = [{ ...items[1].record_citations[0], is_current: false }];
    render(<Harness items={items} />);
    expect(rowOf(/3\+ years of backend experience/)).toHaveTextContent("Added by you");
    expect(rowOf(/shipped services in Quillscript/)).toHaveTextContent("since changed in your record");
  });

  it("renders a requirement's words as text, never as markup (threat T-04-31)", () => {
    const items = evidence();
    items[2].requirement.text = '<img src=x onerror="alert(1)"> Kubernetes';
    const { container } = render(<Harness items={items} />);
    expect(container.querySelector("img")).toBeNull();
    expect(rowOf(/Kubernetes/)).toHaveTextContent('<img src=x onerror="alert(1)">');
  });
});
