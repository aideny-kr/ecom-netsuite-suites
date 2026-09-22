"use client";

import { useRouter } from "next/navigation";
import { MessageSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { primarySlash } from "@/lib/skills";
import type { AgentSkillMetadata } from "@/lib/types";

export function SkillCard({ skill }: { skill: AgentSkillMetadata }) {
  const router = useRouter();
  const slash = primarySlash(skill);
  const chat = skill.execution_surfaces?.find((s) => s.surface === "chat");
  const scheduled = skill.execution_surfaces?.find((s) => s.surface === "scheduled");

  // Populate the composer WITHOUT sending. `compose` is distinct from the
  // existing auto-send `prefill` param (recon uses prefill); the trailing space
  // lets the user type args straight after the command.
  const handleUseInChat = () => {
    router.push(
      "/chat?compose=" + encodeURIComponent(slash + " ") + "&new_session=true",
    );
  };

  return (
    <div className="flex flex-col rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-[15px] font-semibold text-foreground">{skill.name}</h2>
        <code className="shrink-0 rounded-md bg-muted px-2 py-0.5 font-mono text-[12px] text-muted-foreground">
          {slash}
        </code>
      </div>
      <p className="mt-2 flex-1 text-[13px] leading-relaxed text-muted-foreground">
        {skill.description}
      </p>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
        <span className="rounded-md bg-muted px-2 py-1">
          {skill.kind === "playbook" ? "Executable playbook" : skill.kind === "company_instructions" ? "Company instructions" : "Expertise"}
        </span>
        <span className="rounded-md bg-muted px-2 py-1">
          {chat?.status === "available" ? "Chat tools available" : chat?.status === "blocked" ? "Setup needed" : "Readiness not checked"}
        </span>
        {scheduled?.status === "unsupported" && <span className="rounded-md bg-muted px-2 py-1">Not directly schedulable</span>}
      </div>
      {chat?.blockers.map((blocker, i) => (
        <p key={`${blocker.code}-${i}`} className="mt-2 text-xs text-muted-foreground">
          {blocker.message} {blocker.action}
        </p>
      ))}
      {skill.version && (
        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="cursor-pointer">Requirements and provenance</summary>
          <p className="mt-2">{skill.owner} · Version {skill.version.slice(0, 12)}</p>
          <p className="mt-1 break-all">{skill.provenance}</p>
          <p className="mt-2">Inputs: {skill.inputs?.join("; ")}</p>
          <p className="mt-1">Outputs: {skill.outputs?.join("; ")}</p>
          <ul className="mt-2 space-y-1">
            {skill.requirements?.map((r) => <li key={r.key}>{r.satisfied ? "Available" : "Required"}: {r.label}</li>)}
          </ul>
          <p className="mt-2">{skill.readiness_note}</p>
          {scheduled?.blockers.map((b) => <p key={b.code} className="mt-2">{b.message} {b.action}</p>)}
        </details>
      )}
      <Button
        variant="outline"
        size="sm"
        className="mt-4 self-start gap-1.5"
        onClick={handleUseInChat}
      >
        <MessageSquare className="h-3.5 w-3.5" />
        Use in chat
      </Button>
    </div>
  );
}
