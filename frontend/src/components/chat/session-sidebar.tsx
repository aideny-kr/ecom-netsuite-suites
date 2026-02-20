"use client";

import { Plus, MessageSquare, Code2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { ChatSession } from "@/lib/types";

interface SessionSidebarProps {
  sessions: ChatSession[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
}

export function SessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewChat,
}: SessionSidebarProps) {
  return (
    <div className="flex w-[280px] flex-col border-r bg-muted/30">
      <div className="p-4">
        <Button
          variant="outline"
          className="w-full justify-start gap-2 bg-card text-[13px] font-medium shadow-soft"
          onClick={onNewChat}
        >
          <Plus className="h-4 w-4" />
          New Chat
        </Button>
      </div>
      <div className="flex-1 overflow-auto px-3 pb-3 space-y-0.5 scrollbar-thin">
        {sessions.map((session) => (
          <button
            key={session.id}
            onClick={() => onSelectSession(session.id)}
            className={cn(
              "w-full rounded-lg px-3 py-2.5 text-left transition-all duration-150",
              activeSessionId === session.id
                ? "bg-card shadow-soft"
                : "hover:bg-card/50",
            )}
          >
            <div className="flex items-center gap-1.5">
              <p className="truncate text-[13px] font-medium text-foreground">
                {session.title || "New Chat"}
              </p>
              {session.session_type === "workspace" && (
                <Code2 className="h-3 w-3 flex-shrink-0 text-muted-foreground" />
              )}
            </div>
            <p className="truncate text-[11px] text-muted-foreground mt-0.5">
              {new Date(session.created_at).toLocaleDateString()}
            </p>
          </button>
        ))}
        {sessions.length === 0 && (
          <div className="flex flex-col items-center py-12 text-center">
            <MessageSquare className="h-8 w-8 text-muted-foreground/40" />
            <p className="mt-3 text-[13px] text-muted-foreground">
              No conversations yet
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
