import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { DeployConfirmationCard } from "../deploy-confirmation-card";
import type { DeployPreview } from "@/lib/types";

const basePreview: DeployPreview = {
  jti: "11111111-1111-1111-1111-111111111111",
  changeset_id: "22222222-2222-2222-2222-222222222222",
  workspace_id: "33333333-3333-3333-3333-333333333333",
  sandbox_id: "6738075-sb1",
  snapshot_sha: "a".repeat(64),
  manifest_sha: "b".repeat(64),
  require_assertions: false,
  issued_at: "2026-05-19T01:00:00Z",
  expires_at: "2026-05-19T01:10:00Z",
  confirmation_token: "c".repeat(64),
  gates: { validate: "passed", unit_tests: "passed", assertions: "not_required" },
  manifest: [
    { path: "Objects/customscript_a.xml", operation: "create", content_sha: "1".repeat(64), apply_order: 1 },
    { path: "SuiteScripts/edit.js", operation: "modify", content_sha: "2".repeat(64), apply_order: 2 },
    { path: "SuiteScripts/keep.js", operation: "unchanged", content_sha: "3".repeat(64), apply_order: -1 },
  ],
};

describe("DeployConfirmationCard", () => {
  // Test 25: renders manifest entries + sandbox header
  it("renders sandbox target and manifest entries with operations", () => {
    render(
      <DeployConfirmationCard
        preview={basePreview}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    // Sandbox target in the header — count is touchedFiles (excludes unchanged).
    expect(screen.getByText(/Deploy 2 files to/i)).toBeInTheDocument();
    // sandbox_id appears in header + footer, so just assert at least one match.
    expect(screen.getAllByText("6738075-sb1").length).toBeGreaterThan(0);

    // Gate values render
    expect(screen.getByText("not_required")).toBeInTheDocument();
    expect(screen.getAllByText("passed").length).toBeGreaterThanOrEqual(2);

    // Each manifest file path renders
    expect(screen.getByText("Objects/customscript_a.xml")).toBeInTheDocument();
    expect(screen.getByText("SuiteScripts/edit.js")).toBeInTheDocument();
    expect(screen.getByText("SuiteScripts/keep.js")).toBeInTheDocument();
  });

  // Test 26: confirm button calls onConfirm
  it("calls onConfirm when Confirm Deploy is clicked", () => {
    const onConfirm = vi.fn();
    render(
      <DeployConfirmationCard
        preview={basePreview}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /confirm deploy/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  // Test 27: cancel button calls onCancel
  it("calls onCancel when Cancel is clicked", () => {
    const onCancel = vi.fn();
    render(
      <DeployConfirmationCard
        preview={basePreview}
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  // Test 28: disabled prop blocks both buttons
  it("blocks interaction when disabled prop is true", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <DeployConfirmationCard
        preview={basePreview}
        onConfirm={onConfirm}
        onCancel={onCancel}
        disabled
      />,
    );

    const confirmBtn = screen.getByRole("button", { name: /confirm deploy/i });
    const cancelBtn = screen.getByRole("button", { name: /cancel/i });

    expect(confirmBtn).toBeDisabled();
    expect(cancelBtn).toBeDisabled();
  });

  it("collapses long manifests and expands on click", () => {
    const longPreview: DeployPreview = {
      ...basePreview,
      manifest: Array.from({ length: 12 }, (_, i) => ({
        path: `SuiteScripts/file_${i}.js`,
        operation: i === 0 ? "create" : "modify",
        content_sha: "4".repeat(64),
        apply_order: i,
      })),
    };
    render(
      <DeployConfirmationCard
        preview={longPreview}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    // First 5 visible by default; the "show more" toggle reveals the rest.
    expect(screen.getByText("SuiteScripts/file_0.js")).toBeInTheDocument();
    expect(screen.getByText("SuiteScripts/file_4.js")).toBeInTheDocument();
    expect(screen.queryByText("SuiteScripts/file_11.js")).toBeNull();

    const moreBtn = screen.getByRole("button", { name: /show 7 more/i });
    fireEvent.click(moreBtn);
    expect(screen.getByText("SuiteScripts/file_11.js")).toBeInTheDocument();
  });
});
