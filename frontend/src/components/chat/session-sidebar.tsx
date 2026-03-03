"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import {
  Plus,
  MessageSquare,
  Code2,
  Database,
  ChevronDown,
  ChevronRight,
  Pencil,
  Trash2,
  Check,
  X,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { ChatSession } from "@/lib/types";
import {
  useSavedQueries,
  useUpdateSavedQuery,
  useDeleteSavedQuery,
} from "@/hooks/use-saved-queries";
import { useToast } from "@/hooks/use-toast";
import type { SavedQueryResponse } from "@/types/analytics";

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
  const [queriesExpanded, setQueriesExpanded] = useState(true);

  return (
    <div className="flex w-[280px] flex-col border-r bg-muted/30">
      {/* New Chat button */}
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

      {/* Chat Sessions — scrollable */}
      <div className="flex-1 min-h-0 overflow-auto px-3 space-y-0.5 scrollbar-thin">
        {sessions.map((session) => (
          <SessionItem
            key={session.id}
            session={session}
            isActive={activeSessionId === session.id}
            onSelect={() => onSelectSession(session.id)}
            onDeleted={() => {
              if (activeSessionId === session.id) onNewChat();
            }}
          />
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

      {/* Saved Queries — pinned at bottom, own scroll */}
      <div className="border-t">
        <button
          onClick={() => setQueriesExpanded(!queriesExpanded)}
          className="flex w-full items-center gap-1.5 px-5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors"
        >
          {queriesExpanded ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          <Database className="h-3 w-3" />
          Saved Queries
        </button>
        {queriesExpanded && (
          <div className="max-h-[240px] overflow-auto px-3 pb-3 scrollbar-thin">
            <SavedQueriesSection />
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chat Session item with inline edit + delete
// ---------------------------------------------------------------------------

function SessionItem({
  session,
  isActive,
  onSelect,
  onDeleted,
}: {
  session: ChatSession;
  isActive: boolean;
  onSelect: () => void;
  onDeleted: () => void;
}) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const updateMutation = useMutation({
    mutationFn: (title: string) =>
      apiClient.patch<ChatSession>(`/api/v1/chat/sessions/${session.id}`, { title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["chat-session", session.id] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => apiClient.delete(`/api/v1/chat/sessions/${session.id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      onDeleted();
    },
  });

  function startEditing() {
    setEditTitle(session.title || "");
    setIsEditing(true);
    setConfirmDelete(false);
  }

  async function handleSaveTitle() {
    if (!editTitle.trim()) return;
    try {
      await updateMutation.mutateAsync(editTitle.trim());
      toast({ title: "Chat renamed" });
      setIsEditing(false);
    } catch {
      toast({ title: "Failed to rename", variant: "destructive" });
    }
  }

  async function handleDelete() {
    try {
      await deleteMutation.mutateAsync();
      toast({ title: "Chat deleted" });
    } catch {
      toast({ title: "Failed to delete", variant: "destructive" });
    }
  }

  if (confirmDelete) {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-2.5 space-y-2">
        <p className="text-[11px] text-destructive font-medium">
          Delete this chat?
        </p>
        <p className="text-[10px] text-muted-foreground">
          All messages will be permanently removed.
        </p>
        <div className="flex gap-1.5">
          <Button
            variant="destructive"
            size="sm"
            className="h-6 text-[11px] px-2"
            onClick={handleDelete}
            disabled={deleteMutation.isPending}
          >
            {deleteMutation.isPending && (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            )}
            Yes, delete
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 text-[11px] px-2"
            onClick={() => setConfirmDelete(false)}
          >
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  if (isEditing) {
    return (
      <div className="rounded-lg bg-card p-2.5 shadow-soft space-y-1.5">
        <input
          type="text"
          value={editTitle}
          onChange={(e) => setEditTitle(e.target.value)}
          className="w-full rounded-md border bg-background px-2 py-1 text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          autoFocus
          placeholder="Chat title"
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSaveTitle();
            if (e.key === "Escape") setIsEditing(false);
          }}
        />
        <div className="flex gap-1">
          <Button
            variant="default"
            size="sm"
            className="h-5 text-[10px] px-1.5"
            onClick={handleSaveTitle}
            disabled={!editTitle.trim() || updateMutation.isPending}
          >
            {updateMutation.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Check className="h-3 w-3" />
            )}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-5 text-[10px] px-1.5"
            onClick={() => setIsEditing(false)}
          >
            <X className="h-3 w-3" />
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "group flex items-center rounded-lg px-3 py-2.5 text-left transition-all duration-150 cursor-pointer",
        isActive ? "bg-card shadow-soft" : "hover:bg-card/50",
      )}
      onClick={onSelect}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <p className="truncate text-[13px] font-medium text-foreground">
            {session.title || "New Chat"}
          </p>
          {session.session_type === "workspace" && (
            <Code2 className="h-3 w-3 flex-shrink-0 text-muted-foreground" />
          )}
        </div>
        <p className="truncate text-[11px] text-muted-foreground mt-0.5">
          {new Date(session.updated_at).toLocaleDateString()}
        </p>
      </div>
      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-1">
        <button
          onClick={(e) => {
            e.stopPropagation();
            startEditing();
          }}
          className="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted"
          title="Rename"
        >
          <Pencil className="h-3 w-3" />
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setConfirmDelete(true);
          }}
          className="rounded p-1 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
          title="Delete"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Saved Queries section inside the sidebar
// ---------------------------------------------------------------------------

function SavedQueriesSection() {
  const { data: queries, isLoading } = useSavedQueries();
  const updateMutation = useUpdateSavedQuery();
  const deleteMutation = useDeleteSavedQuery();
  const { toast } = useToast();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  function startEditing(query: SavedQueryResponse) {
    setEditingId(query.id);
    setEditName(query.name);
    setEditDescription(query.description || "");
    setConfirmDeleteId(null);
  }

  async function handleSaveEdit(id: string) {
    if (!editName.trim()) return;
    try {
      await updateMutation.mutateAsync({
        id,
        name: editName.trim(),
        description: editDescription.trim() || null,
      });
      toast({ title: "Query updated" });
      setEditingId(null);
    } catch (err) {
      toast({
        title: "Failed to update",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleDelete(id: string) {
    try {
      await deleteMutation.mutateAsync(id);
      toast({ title: "Query deleted" });
      setConfirmDeleteId(null);
    } catch (err) {
      toast({
        title: "Failed to delete",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  if (isLoading) {
    return (
      <div className="px-2 py-3 text-[11px] text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!queries?.length) {
    return (
      <div className="px-2 py-3 text-[11px] text-muted-foreground">
        No saved queries
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {queries.map((query) => (
        <div key={query.id} className="group">
          {editingId === query.id ? (
            <div className="rounded-lg bg-card p-2.5 shadow-soft space-y-1.5">
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                className="w-full rounded-md border bg-background px-2 py-1 text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                autoFocus
                placeholder="Query name"
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSaveEdit(query.id);
                  if (e.key === "Escape") setEditingId(null);
                }}
              />
              <input
                type="text"
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                placeholder="Description (optional)"
                className="w-full rounded-md border bg-background px-2 py-1 text-[11px] text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSaveEdit(query.id);
                  if (e.key === "Escape") setEditingId(null);
                }}
              />
              <div className="flex gap-1">
                <Button
                  variant="default"
                  size="sm"
                  className="h-5 text-[10px] px-1.5"
                  onClick={() => handleSaveEdit(query.id)}
                  disabled={!editName.trim() || updateMutation.isPending}
                >
                  {updateMutation.isPending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Check className="h-3 w-3" />
                  )}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-5 text-[10px] px-1.5"
                  onClick={() => setEditingId(null)}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ) : confirmDeleteId === query.id ? (
            <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-2.5 space-y-2">
              <p className="text-[11px] text-destructive font-medium">
                Delete &quot;{query.name}&quot;?
              </p>
              <div className="flex gap-1.5">
                <Button
                  variant="destructive"
                  size="sm"
                  className="h-6 text-[11px] px-2"
                  onClick={() => handleDelete(query.id)}
                  disabled={deleteMutation.isPending}
                >
                  {deleteMutation.isPending ? (
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  ) : null}
                  Yes, delete
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-[11px] px-2"
                  onClick={() => setConfirmDeleteId(null)}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center rounded-lg px-3 py-2 hover:bg-card/50 transition-all duration-150">
              <div className="min-w-0 flex-1">
                <p className="truncate text-[12px] font-medium text-foreground">
                  {query.name}
                </p>
                {query.description && (
                  <p className="truncate text-[10px] text-muted-foreground mt-0.5">
                    {query.description}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-1">
                <button
                  onClick={() => startEditing(query)}
                  className="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted"
                  title="Edit"
                >
                  <Pencil className="h-3 w-3" />
                </button>
                <button
                  onClick={() => {
                    setConfirmDeleteId(query.id);
                    setEditingId(null);
                  }}
                  className="rounded p-1 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                  title="Delete"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
