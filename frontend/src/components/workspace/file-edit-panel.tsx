"use client";

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { Button } from "@/components/ui/button";
import { useProposeFileEdit } from "@/hooks/use-workspace";
import type { FileReadResponse } from "@/lib/types";
import { CodeViewer } from "./code-viewer";

export type FileDrafts = Record<string, { content: string; baseline: string }>;

export function FileEditPanel({ workspaceId, file, drafts, onReview }: {
  workspaceId: string;
  file: FileReadResponse;
  drafts: MutableRefObject<FileDrafts>;
  onReview: (changesetId: string) => void;
}) {
  const key = `${workspaceId}:${file.id}`;
  const current = useRef(key);
  current.current = key;
  const mounted = useRef(true);
  const [, redraw] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const propose = useProposeFileEdit();
  const editable = file.can_edit === true && !file.truncated && !!file.sha256_hash;
  const draft = drafts.current[key];

  useEffect(() => {
    mounted.current = true;
    const warn = (event: BeforeUnloadEvent) => {
      if (Object.keys(drafts.current).length) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => { mounted.current = false; window.removeEventListener("beforeunload", warn); };
  }, [drafts]);

  async function save() {
    if (!draft || !editable || pending) return;
    const capturedKey = key;
    setPending(true);setError(null);
    try {
      const result = await propose.mutateAsync({
        workspaceId, fileId: file.id, content: draft.content, baseline_sha256: draft.baseline,
      });
      delete drafts.current[capturedKey];
      if (mounted.current && current.current === capturedKey) onReview(result.changeset_id);
    } catch (error) {
      if (mounted.current) setError({ key: capturedKey, message: error instanceof Error ? error.message : "Unable to save draft" });
    } finally {
      if (mounted.current) { setPending(false);redraw(n => n + 1); }
    }
  }

  return <div className="flex h-full min-w-0 flex-col">
    <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
      <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">
        {draft ? "Unsaved edit · Save a draft to preserve it for review. Applying and deploying require separate steps." : "Edits are saved as drafts for review."}
      </p>
      {editable && (draft ? <>
        <Button size="sm" variant="ghost" disabled={pending} onClick={() => { delete drafts.current[key];setError(null);redraw(n=>n+1); }}>Discard edit</Button>
        <Button size="sm" disabled={pending || draft.content === file.content} onClick={save}>{pending ? "Saving…" : "Save draft for review"}</Button>
      </> : <Button size="sm" variant="outline" onClick={() => {
        drafts.current[key] = { content: file.content, baseline: file.sha256_hash! };
        redraw(n=>n+1);
      }}>Edit code</Button>)}
    </div>
    {error?.key === key && <p role="alert" className="px-4 py-2 text-[13px] text-destructive">{error.message}</p>}
    {draft && editable ? <textarea
      aria-label={`Edit ${file.path}`} spellCheck={false} disabled={pending}
      className="min-h-48 w-full min-w-0 flex-1 resize-none bg-background p-4 font-mono text-[13px] leading-relaxed focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-primary"
      value={draft.content} onChange={event => { drafts.current[key] = { ...draft, content: event.target.value };redraw(n=>n+1); }}
    /> : <CodeViewer content={file.content} filePath={file.path} />}
  </div>;
}
