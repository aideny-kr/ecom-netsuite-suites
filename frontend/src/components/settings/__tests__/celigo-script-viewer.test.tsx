import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

// Task 10 — script viewer (mockup screen 04) presentational body.
//
// Task 18 — `CeligoScriptViewerDialog` (the loading/error-gating wrapper
// this file used to test through) is deleted along with its last caller,
// `celigo-flow-map.tsx`. `CeligoScriptViewerBody` takes `script` as a plain
// prop and calls no hook itself, so this file renders it directly — no
// QueryClient, no `use-celigo-flows` mock. Its loading/error states, and its
// re-homing as a right-panel drawer, are already covered where it is
// actually consumed today: `celigo-script-drawer.test.tsx` (Task 17).

import { CeligoScriptViewerBody } from "../celigo-script-viewer";

const siteA = {
  flow_id: "flow-1",
  flow_name: "Inventory Sync",
  integration_id: "int-1",
  flow_step_id: "step-1",
  flow_step_role: "processor",
  flow_step_adaptor_type: "NetSuiteDistributedImport",
  script_celigo_id: "scr-1",
  json_path: "pageProcessors[0].transform.script",
  function_name: "transform",
  site_type: "transform",
};

const siteB = {
  ...siteA,
  json_path: "hooks.preSavePage",
  function_name: "preSave",
  site_type: "hook",
};

const baseScript = {
  id: "scr-local-1",
  dedup_key: "dk-1",
  name: "BigQuery Data Warehouse Script[v1.1.0]",
  content: "function transform(record) { return record; }",
  content_hash: "hash-1",
  copies_count: 1,
  attachment_count: 2,
  integration_count: 1,
  content_diverged: false,
  used_by: [siteA, siteB],
};

function cloneFamily(overrides: Partial<typeof baseScript> = {}) {
  const sites = Array.from({ length: 20 }, (_, i) => ({
    ...siteA,
    flow_id: `flow-${i}`,
    flow_name: `Flow ${i}`,
    flow_step_id: `step-${i}`,
    script_celigo_id: `scr-${i}`,
  }));
  return {
    ...baseScript,
    copies_count: 20,
    attachment_count: 20,
    used_by: sites,
    ...overrides,
  };
}

describe("CeligoScriptViewerBody — card head", () => {
  // The pill format is "N copies[ · diverged]" (family) plus a separate
  // "N sites · M flows" pill (used_by-derived). `baseScript` has both sites
  // on the SAME flow_id ("flow-1"), so the sites/flows pill reads
  // "2 sites · 1 flow" -- each half is pluralised on its OWN count.
  it("shows the script name, the hook chip, and the copies/sites pills", () => {
    render(<CeligoScriptViewerBody script={baseScript} />);
    expect(screen.getByText("BigQuery Data Warehouse Script[v1.1.0]")).toBeInTheDocument();
    expect(screen.getByText("HK transform")).toBeInTheDocument();
    expect(screen.getByText("1 copies")).toBeInTheDocument();
    expect(screen.getByText("2 sites · 1 flow")).toBeInTheDocument();
  });

  it("pluralises each half of the sites/flows pill on its own count", () => {
    // Gate fix wave, item 11: a single-site script read "1 sites · 1 flows".
    render(<CeligoScriptViewerBody script={{ ...baseScript, used_by: [siteA] }} />);
    expect(screen.getByText("1 site · 1 flow")).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerBody — attachment table", () => {
  it("renders BOTH sites for a script attached at both transform.script and hooks.preSavePage", () => {
    render(<CeligoScriptViewerBody script={baseScript} />);
    expect(screen.getByText("pageProcessors[0].transform.script")).toBeInTheDocument();
    expect(screen.getByText("hooks.preSavePage")).toBeInTheDocument();
    expect(screen.getAllByText("Inventory Sync")).toHaveLength(2);
  });

  it("collapses ~20 clone copies into a single summary row instead of 20 explicit rows", () => {
    render(<CeligoScriptViewerBody script={cloneFamily()} />);
    const rows = screen.getAllByRole("row");
    // header row + 1 explicit attachment row + 1 collapse row
    expect(rows).toHaveLength(3);
    expect(screen.getByText(/19 further copies/i)).toBeInTheDocument();
  });

  it("guards an empty-string function_name the same as null (not just missing)", () => {
    render(
      <CeligoScriptViewerBody script={{ ...baseScript, used_by: [{ ...siteA, function_name: "" }] }} />,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("shows a genuinely-empty state (distinct from a loading/error state) when a script has no recorded attachment sites", () => {
    render(<CeligoScriptViewerBody script={{ ...baseScript, used_by: [] }} />);
    expect(screen.getByText(/no attachment sites recorded/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerBody — content_diverged correction", () => {
  it("says 'identical source' when content_diverged is false", () => {
    render(<CeligoScriptViewerBody script={cloneFamily({ content_diverged: false })} />);
    expect(screen.getByText(/identical source/i)).toBeInTheDocument();
  });

  it("says the copies differ, and that the shown source is only this copy's own version, when content_diverged is true", () => {
    render(<CeligoScriptViewerBody script={cloneFamily({ content_diverged: true })} />);
    expect(screen.queryByText(/identical source/i)).not.toBeInTheDocument();
    expect(screen.getByText(/differ/i)).toBeInTheDocument();
    expect(screen.getByText(/this copy's own version/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerBody — untrusted content", () => {
  it("renders the script source and the untrusted-content banner", () => {
    render(<CeligoScriptViewerBody script={baseScript} />);
    // The syntax highlighter splits source into multiple token spans, so a
    // single-node text match won't find it -- assert against the full
    // rendered body text instead.
    expect(document.body.textContent).toContain("function transform(record) { return record; }");
    // N2's exact, project-wide banner copy.
    expect(
      screen.getByText("Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant."),
    ).toBeInTheDocument();
  });

  // Fix round 1 -- `??` does not catch `""`. `celigo-script-viewer.tsx`
  // defines `displayOr` specifically to guard that, and routes every OTHER
  // optional field through it -- `content` was the one field left on `??`.
  it("shows the 'no source recorded' placeholder when content is an empty string, not a blank code block", () => {
    render(<CeligoScriptViewerBody script={{ ...baseScript, content: "" }} />);
    expect(document.body.textContent).toContain("// No source recorded for this script.");
  });
});
