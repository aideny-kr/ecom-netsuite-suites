"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { apiClient } from "@/lib/api-client";

interface Usage {
  uses: { name: string; href: string; binding: string; active: boolean }[];
  visibility_limited: boolean;
  coverage: string;
}

/** Mounted inside an explicit disclosure/dialog; no provider requests or writes. */
export function ConnectionUsage({ kind, id }: { kind: "api" | "mcp"; id: string }) {
  const [result, setResult] = useState<Usage>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let current = true;
    setResult(undefined); setFailed(false);
    apiClient.get<Usage>(`/api/v1/connections/usage/${kind}/${encodeURIComponent(id)}`)
      .then((value) => { if (current) setResult(value); })
      .catch(() => { if (current) setFailed(true); });
    return () => { current = false; };
  }, [kind, id]);
  if (failed) return <p role="alert" className="text-[13px] text-destructive">Dependencies could not be checked. Review workflows and source bindings before disconnecting.</p>;
  if (!result) return <p role="status" className="text-[13px] text-muted-foreground">Checking saved dependencies…</p>;
  return <div className="space-y-2 text-[13px]">
    {result.uses.length ? <ul className="space-y-2">{result.uses.map((use, index) => <li key={`${use.href}:${index}`}><Link className="text-primary underline" href={use.href}>{use.name}</Link><span className="text-muted-foreground"> · {use.binding} · {use.active ? "enabled" : "paused"}</span></li>)}</ul> : <p>No matching dependencies found in the visible supported bindings.</p>}
    {result.visibility_limited && <p>Some dependency categories are unavailable with your current access.</p>}
    <p className="text-muted-foreground">{result.coverage}</p>
    <Link href="/skills" className="text-primary underline">Review skills and their requirements</Link>
  </div>;
}
