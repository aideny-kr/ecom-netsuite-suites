import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

/**
 * Task 5 (Celigo Scripts view) — `DiffViewer` gains two optional props the
 * Scripts detail pane's compare mode needs: `sideBySide` (the mock's
 * "Side by side / Inline" toggle, spec §3.3) and `language` (a script has
 * no file path to derive a Monaco language from, spec §3.3's "or a
 * `language` override so a script does not need a file path"). Both are
 * additive and default to today's behaviour — see this file's own docstring
 * on why every existing caller (`workspace/page.tsx`, which passes only
 * `original`/`modified`/`filePath`) is unaffected.
 *
 * `next/dynamic` is mocked to a plain pass-through so this test exercises
 * `DiffViewer`'s OWN prop computation (which `language`/`renderSideBySide`
 * it hands to the underlying editor) without pulling in real Monaco.
 */
vi.mock("next/dynamic", () => ({
  default: () => {
    return function MockMonacoDiffEditor(props: Record<string, unknown>) {
      return <div data-testid="monaco-diff" data-props={JSON.stringify(props)} />;
    };
  },
}));

import { DiffViewer } from "../diff-viewer";

function editorProps(): { language: string; options: { renderSideBySide: boolean } } {
  return JSON.parse(screen.getByTestId("monaco-diff").getAttribute("data-props")!);
}

describe("DiffViewer", () => {
  it("defaults to side-by-side rendering with a filePath-derived language", () => {
    render(<DiffViewer original="a" modified="b" filePath="preMap.js" />);
    const props = editorProps();
    expect(props.options.renderSideBySide).toBe(true);
    expect(props.language).toBe("javascript");
  });

  it("renders inline when sideBySide={false}", () => {
    render(<DiffViewer original="a" modified="b" filePath="preMap.js" sideBySide={false} />);
    expect(editorProps().options.renderSideBySide).toBe(false);
  });

  it("an explicit language overrides the one derived from filePath", () => {
    // A script has no real file path/extension to derive a language from
    // (spec §3.3) — the caller states the language directly instead.
    render(<DiffViewer original="a" modified="b" filePath="ns_sales_order_premap" language="javascript" />);
    expect(editorProps().language).toBe("javascript");
  });
});
