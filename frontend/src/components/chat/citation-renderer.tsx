"use client";

import React from "react";

interface Props {
  text: string;
  sources: Record<string, string> | undefined;
}

// Match [label] NOT immediately followed by `(` — skips markdown links `[text](url)`.
const CITATION_RE = /\[([^\[\]\n]+)\](?!\()/g;

export function CitationRenderer({ text, sources }: Props) {
  if (!sources || Object.keys(sources).length === 0) {
    return <>{text}</>;
  }
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  // Array.from() works under ES5 target without downlevelIteration.
  for (const match of Array.from(text.matchAll(CITATION_RE))) {
    const idx = match.index ?? 0;
    const full = match[0];
    const name = match[1];
    const url = sources[name];
    if (idx > lastIdx) {
      parts.push(text.slice(lastIdx, idx));
    }
    if (url) {
      parts.push(
        <a
          key={`${idx}-${name}`}
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline hover:no-underline"
        >
          {name}
        </a>,
      );
    } else {
      parts.push(full);
    }
    lastIdx = idx + full.length;
  }
  if (lastIdx < text.length) {
    parts.push(text.slice(lastIdx));
  }
  return <>{parts}</>;
}
