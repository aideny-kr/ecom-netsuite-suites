"use client";

import { MessageSquare, Settings, X } from "lucide-react";
import { cn } from "@/lib/utils";

interface AgentChatHeaderProps {
  agentId: string;
  agentName: string;
  activeTab: "chat" | "config";
  onTabChange: (tab: "chat" | "config") => void;
  onExit?: () => void;
}

export function AgentChatHeader({ agentId, agentName, activeTab, onTabChange, onExit }: AgentChatHeaderProps) {
  return (
    <div className="flex items-center justify-between px-5 py-2 border-b border-border/50 bg-card/50">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-full bg-emerald-500" />
        <span className="text-[13px] font-medium text-foreground">{agentName}</span>
        <span className="text-[10px] bg-[var(--chat-accent)]/15 text-[var(--chat-accent)] px-2 py-0.5 rounded-full font-medium">
          Active
        </span>
      </div>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onTabChange("chat")}
          className={cn(
            "px-3 py-1 rounded-md text-[12px] font-medium transition-colors",
            activeTab === "chat"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground hover:bg-muted/50",
          )}
        >
          <MessageSquare className="h-3 w-3 inline mr-1" /> Chat
        </button>
        <button
          onClick={() => onTabChange("config")}
          className={cn(
            "px-3 py-1 rounded-md text-[12px] font-medium transition-colors",
            activeTab === "config"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground hover:bg-muted/50",
          )}
        >
          <Settings className="h-3 w-3 inline mr-1" /> Config
        </button>
        {onExit && (
          <button
            onClick={onExit}
            className="ml-2 p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
