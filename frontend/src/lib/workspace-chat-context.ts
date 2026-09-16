/** Use one enrichment path for the composer and WebMCP. Never silently shorten
 * a developer's message; the caller can preserve it and show the length error. */
export function workspaceChatContent(content: string, filePath: string | undefined, maxChars: number) {
  const enriched = filePath ? `[Currently viewing file: ${filePath}]\n\n${content}` : content;
  if (enriched.length > maxChars) throw new Error(`Message including file context exceeds the ${maxChars}-character limit.`);
  return enriched;
}
