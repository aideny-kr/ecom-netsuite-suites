/**
 * Behavioral tests for ChangeProposalCard — the card the agent renders inline
 * in chat after calling workspace_propose_patch.
 *
 * Two regressions worth pinning:
 *   1. The full card (diff preview + Approve/Apply buttons) must render when
 *      result_summary contains a parseable changeset_id JSON. Pre-PR #85 the
 *      backend collapsed the summary to "Returned 1 row" and the card fell
 *      back to a buttonless stub with no diff.
 *   2. The unified_diff must come through from step.params.unified_diff and
 *      be visible in the inline DiffLines preview (the +/- lines the user
 *      reviews before approving). The diff body lives on params, not on the
 *      result, so it survives summary-allowlisting.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

import { ChangeProposalCard } from "@/components/chat/change-proposal-card";
import type { ToolCallStep } from "@/lib/types";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    // Default: pretend the changeset row says draft. The card composes status
    // from useChangeset(id).data?.status ?? result.diff_status ?? "draft" so
    // unless we mock the GET we'd render the "ok" diff_status as status and
    // see no action buttons.
    get: vi.fn().mockResolvedValue({ id: "cs-real", status: "draft" }),
    post: vi.fn().mockResolvedValue(undefined),
  },
}));

import { apiClient } from "@/lib/api-client";

function renderCard(step: ToolCallStep, opts: { changesetStatus?: string } = {}) {
  if (opts.changesetStatus !== undefined) {
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "cs-real",
      status: opts.changesetStatus,
    });
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ChangeProposalCard
        step={step}
        workspaceId="ws-1"
        onViewDiff={vi.fn()}
        onChangesetAction={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

const realDiff = `@@ -19,6 +19,7 @@

+        //TODO
         const map = (mapContext) => {`;

const validResultSummary = JSON.stringify({
  changeset_id: "cs-real",
  patch_id: "p-1",
  operation: "modify",
  diff_status: "ok",
  risk_summary: "low",
});

function makeStep(overrides: Partial<ToolCallStep> = {}): ToolCallStep {
  return {
    step: 2,
    tool: "workspace_propose_patch",
    params: {
      workspace_id: "ws-1",
      file_path: "SuiteScripts/foo.js",
      unified_diff: realDiff,
      title: "Add //TODO mark on line 20",
    },
    result_summary: validResultSummary,
    duration_ms: 234,
    ...overrides,
  } as ToolCallStep;
}

describe("ChangeProposalCard — diff display + actions", () => {
  it("renders the unified diff inline when result_summary has a parseable changeset_id", () => {
    const { container } = renderCard(makeStep());

    // Header + file
    expect(screen.getByText("Add //TODO mark on line 20")).toBeInTheDocument();
    expect(screen.getByText("SuiteScripts/foo.js")).toBeInTheDocument();

    // Diff toggle is present (button labelled "Diff preview")
    expect(screen.getByText("Diff preview")).toBeInTheDocument();

    // The DiffLines component renders each line of unified_diff in its own
    // <div> inside a <pre>. Testing-library normalizes whitespace, so we
    // check the rendered text content directly. The +TODO addition and the
    // hunk header are the key diff signals the user must see before
    // approving.
    const rendered = container.textContent ?? "";
    expect(rendered).toContain("//TODO");
    expect(rendered).toContain("@@ -19,6 +19,7 @@");
    expect(rendered).toContain("const map = (mapContext) => {");
  });

  it("renders Approve/Apply controls when the card has a parseable changeset_id", async () => {
    renderCard(makeStep());

    // useChangeset resolves to {status: "draft"} → draft branch renders
    // Submit + Discard. Wait because useQuery fires async.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Submit for Review/i }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /Discard/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /View Full Diff/i }),
    ).toBeInTheDocument();
  });

  it("renders Approve/Reject when the changeset is in pending_review", async () => {
    renderCard(makeStep(), { changesetStatus: "pending_review" });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Approve/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /Reject/i })).toBeInTheDocument();
  });

  it("renders Apply Changes when the changeset is approved", async () => {
    renderCard(makeStep(), { changesetStatus: "approved" });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Apply Changes/i }),
      ).toBeInTheDocument(),
    );
  });

  it("falls back to a stub with no diff when result_summary cannot be parsed", () => {
    // Regression we shipped a fix for: pre-PR #85 the summary was
    // "Returned 1 row" so parseResult returned null and the card rendered
    // this fallback. Locking the behavior means a future regression that
    // accidentally strips changeset_id from the summary will be caught.
    renderCard(makeStep({ result_summary: "Returned 1 row" }));

    expect(screen.getByText(/Returned 1 row/)).toBeInTheDocument();
    // No diff preview button in the fallback
    expect(screen.queryByText("Diff preview")).not.toBeInTheDocument();
    // No action buttons either
    expect(screen.queryByRole("button", { name: /Submit for Review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/i })).not.toBeInTheDocument();
  });
});
