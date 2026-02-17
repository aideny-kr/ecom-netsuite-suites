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
}

export function DiffViewer({ original, modified, filePath }: DiffViewerProps) {
  const language = getLanguageFromPath(filePath);

  return (
    <MonacoDiffEditor
      height="100%"
      language={language}
      original={original}
      modified={modified}
      options={{
        readOnly: true,
        renderSideBySide: true,
        minimap: { enabled: false },
      }}
      theme="vs-dark"
    />
  );
}
