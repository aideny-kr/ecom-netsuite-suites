import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ExportMenu } from "@/components/reconciliation/export-menu";

describe("ExportMenu", () => {
  it("is closed by default and opens on trigger click", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("renders exactly two entries: CSV and Excel", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const items = screen.getAllByRole("menuitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent(/csv.*visible columns/i);
    expect(items[1]).toHaveTextContent(/excel.*formatted sheet/i);
  });

  it("builds the section=groups href with format for each entry, no group_key/currency/action", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const [csv, xlsx] = screen.getAllByRole("menuitem");
    expect(csv).toHaveAttribute(
      "href",
      "/api/v1/reconciliation/runs/r1/export?section=groups&format=csv",
    );
    expect(xlsx).toHaveAttribute(
      "href",
      "/api/v1/reconciliation/runs/r1/export?section=groups&format=xlsx",
    );
  });

  it("builds the section=proposals href with group_key + currency, URL-encoding the group_key", () => {
    render(
      <ExportMenu
        runId="r1"
        params={{ section: "proposals", group_key: "fees:book_fee_line:deposit", currency: "EUR" }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const [csv] = screen.getAllByRole("menuitem");
    expect(csv).toHaveAttribute(
      "href",
      `/api/v1/reconciliation/runs/r1/export?section=proposals&format=csv&group_key=${encodeURIComponent(
        "fees:book_fee_line:deposit",
      )}&currency=EUR`,
    );
  });

  it("builds the section=proposals href with action=needs_human and no group_key/currency", () => {
    render(<ExportMenu runId="r1" params={{ section: "proposals", action: "needs_human" }} />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const [csv] = screen.getAllByRole("menuitem");
    expect(csv).toHaveAttribute(
      "href",
      "/api/v1/reconciliation/runs/r1/export?section=proposals&format=csv&action=needs_human",
    );
  });

  it("builds the section=results href for the classic view", () => {
    render(<ExportMenu runId="r9" params={{ section: "results" }} />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const [csv] = screen.getAllByRole("menuitem");
    expect(csv).toHaveAttribute(
      "href",
      "/api/v1/reconciliation/runs/r9/export?section=results&format=csv",
    );
  });

  it("closes on outside click", () => {
    render(
      <div>
        <ExportMenu runId="r1" params={{ section: "groups" }} />
        <button type="button">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("button", { name: /outside/i }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes on Escape", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("toggles closed when clicking the trigger again", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    const trigger = screen.getByRole("button", { name: /export/i });
    fireEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("keeps the trigger and both entries keyboard-focusable", () => {
    render(<ExportMenu runId="r1" params={{ section: "groups" }} />);
    const trigger = screen.getByRole("button", { name: /export/i });
    trigger.focus();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    const items = screen.getAllByRole("menuitem");
    items[0].focus();
    expect(items[0]).toHaveFocus();
    items[1].focus();
    expect(items[1]).toHaveFocus();
  });
});
