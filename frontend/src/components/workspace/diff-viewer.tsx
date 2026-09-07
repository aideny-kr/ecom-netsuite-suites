"use client";

import dynamic from "next/dynamic";

const MonacoDiffEditor = dynamic(
  () => import("@monaco-editor/react").then((m) => m.DiffEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center bg-muted/30">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    ),
  },
);

function getLanguageFromPath(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const langMap: Record<string, string> = {
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    jsx: "javascript",
    json: "json",
    xml: "xml",
    html: "html",
    css: "css",
    sql: "sql",
    py: "python",
    md: "markdown",
    yaml: "yaml",
    yml: "yaml",
  };
  return langMap[ext] || "plaintext";
}

interface DiffViewerProps {
  original: string;
  modified: string;
  filePath: string;
  /** Renders left/right panes side by side (Monaco's `renderSideBySide`)
   * when true (the default, today's only behaviour), or a single inline
   * pane when false — the Celigo Scripts detail pane's compare mode
   * "Side by side / Inline" toggle (spec §3.3). */
  sideBySide?: boolean;
  /** Overrides the language Monaco derives from `filePath`'s extension.
   * A customer script has no real file path to extract one from (it's a
   * clone family's synthetic name), so the caller states the language
   * directly instead of this component guessing "plaintext" off a
   * path with no extension. */
  language?: string;
}

export function DiffViewer({ original, modified, filePath, sideBySide = true, language }: DiffViewerProps) {
  const resolvedLanguage = language ?? getLanguageFromPath(filePath);

  return (
    <MonacoDiffEditor
      height="100%"
      language={resolvedLanguage}
      original={original}
      modified={modified}
      options={{
        readOnly: true,
        renderSideBySide: sideBySide,
        minimap: { enabled: false },
      }}
      theme="vs-dark"
    />
  );
}
