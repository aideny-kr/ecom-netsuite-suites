"use client";

import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

export default function CodeHighlight({
  content,
  language,
}: {
  content: string;
  language: string;
}) {
  return (
    <SyntaxHighlighter
      style={oneDark}
      language={language}
      PreTag="div"
      customStyle={{
        margin: 0,
        background: "hsl(var(--sidebar-bg))",
        borderRadius: 0,
        fontSize: "13px",
        lineHeight: "1.5",
      }}
    >
      {content}
    </SyntaxHighlighter>
  );
}
