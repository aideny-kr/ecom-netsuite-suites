"use client";

import { useMemo } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

interface CodeViewerProps {
  content: string;
  filePath: string;
}

const EXT_TO_LANGUAGE: Record<string, string> = {
  ".js": "javascript",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".jsx": "jsx",
  ".json": "json",
  ".xml": "xml",
  ".html": "html",
  ".css": "css",
  ".sql": "sql",
  ".py": "python",
  ".md": "markdown",
  ".yml": "yaml",
  ".yaml": "yaml",
  ".sh": "bash",
  ".txt": "text",
};

function getLanguage(filePath: string): string {
  const ext = filePath.slice(filePath.lastIndexOf(".")).toLowerCase();
  return EXT_TO_LANGUAGE[ext] || "javascript";
}

export function CodeViewer({ content, filePath }: CodeViewerProps) {
  const language = useMemo(() => getLanguage(filePath), [filePath]);

  return (
    <div className="h-full overflow-auto">
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        showLineNumbers
        wrapLongLines
        customStyle={{
          margin: 0,
          padding: "1rem",
          fontSize: "13px",
          lineHeight: "1.6",
          background: "#1e1e1e",
          minHeight: "100%",
        }}
        lineNumberStyle={{
          minWidth: "3em",
          paddingRight: "1em",
          color: "#858585",
          userSelect: "none",
        }}
      >
        {content}
      </SyntaxHighlighter>
    </div>
  );
}
