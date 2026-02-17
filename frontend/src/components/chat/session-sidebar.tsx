"use client";

import { Plus } from "lucide-react";
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
    <div className="flex w-64 flex-col border-r bg-muted/30">
      <div className="border-b p-3">
        <Button
          variant="outline"
          className="w-full justify-start gap-2"
          onClick={onNewChat}
        >
          <Plus className="h-4 w-4" />
          New Chat
        </Button>
      </div>
      <div className="flex-1 overflow-auto p-2 space-y-1">
        {sessions.map((session) => (
          <button
            key={session.id}
            onClick={() => onSelectSession(session.id)}
            className={cn(
              "w-full rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-accent",
              activeSessionId === session.id && "bg-accent text-accent-foreground",
            )}
          >
            <p className="truncate font-medium">
              {session.title || "New Chat"}
            </p>
            <p className="truncate text-xs text-muted-foreground">
              {new Date(session.created_at).toLocaleDateString()}
            </p>
          </button>
        ))}
        {sessions.length === 0 && (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">
            No conversations yet
          </p>
        )}
      </div>
    </div>
  );
}
