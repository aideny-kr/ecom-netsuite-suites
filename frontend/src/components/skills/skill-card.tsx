"use client";

import { useRouter } from "next/navigation";
import { MessageSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { AgentSkillMetadata } from "@/lib/types";

// Each skill's first `/`-prefixed trigger is its slash command; fall back to the
// first trigger of any shape. Mirrors chat-input.tsx's selection so the page and
// the composer's slash-menu can never disagree on a skill's primary command.
export function primarySlash(skill: AgentSkillMetadata): string {
  return skill.triggers.find((t) => t.startsWith("/")) ?? skill.triggers[0];
}

export function SkillCard({ skill }: { skill: AgentSkillMetadata }) {
  const router = useRouter();
  const slash = primarySlash(skill);

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
