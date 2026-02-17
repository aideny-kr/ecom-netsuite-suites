"use client";

import dynamic from "next/dynamic";

const MonacoEditor = dynamic(
  () => import("@monaco-editor/react").then((m) => m.default),
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

interface CodeViewerProps {
  content: string;
  filePath: string;
}

export function CodeViewer({ content, filePath }: CodeViewerProps) {
  const language = getLanguageFromPath(filePath);

  return (
    <MonacoEditor
      height="100%"
      language={language}
      value={content}
      options={{
        readOnly: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 13,
        wordWrap: "on",
        lineNumbers: "on",
      }}
      theme="vs-dark"
    />
  );
}
