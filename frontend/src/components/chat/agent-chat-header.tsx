"use client";

import { Settings, X } from "lucide-react";

interface AgentChatHeaderProps {
  agentId: string;
  agentName: string;
  onOpenSettings?: () => void;
  onExit?: () => void;
}

export function AgentChatHeader({ agentId, agentName, onOpenSettings, onExit }: AgentChatHeaderProps) {
  return (
    <div className="flex h-10 items-center justify-between border-b bg-card px-4">
      <div className="flex items-center gap-2">
        <div className="h-2 w-2 rounded-full bg-green-500" />
        <span className="text-[13px] font-medium text-foreground">{agentName}</span>
        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
          Pinned
        </span>
      </div>
      <div className="flex items-center gap-1">
        {onOpenSettings && (
          <button
            onClick={onOpenSettings}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title="Agent settings"
          >
            <Settings className="h-3.5 w-3.5" />
          </button>
        )}
        {onExit && (
          <button
            onClick={onExit}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title="Exit agent mode"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
