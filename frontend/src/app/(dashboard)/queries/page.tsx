"use client";

import { useState } from "react";
import {
  useSavedQueries,
  useCreateSavedQuery,
  useDeleteSavedQuery,
} from "@/hooks/use-saved-queries";
import { QueryPreviewModal } from "@/components/analytics/QueryPreviewModal";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import type { SavedQueryResponse } from "@/types/analytics";
import {
  Database,
  Plus,
  Trash2,
  Table as TableIcon,
  Calendar,
  X,
  Loader2,
} from "lucide-react";

export default function QueriesPage() {
  const { data: queries, isLoading } = useSavedQueries();
  const deleteMutation = useDeleteSavedQuery();
  const { toast } = useToast();

  const [selectedQuery, setSelectedQuery] = useState<SavedQueryResponse | null>(
    null,
  );
  const [showForm, setShowForm] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  async function handleDelete(id: string) {
    try {
      await deleteMutation.mutateAsync(id);
      toast({ title: "Query deleted" });
      setConfirmDeleteId(null);
    } catch (err) {
      toast({
        title: "Failed to delete query",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight text-foreground">
            Saved Queries
          </h2>
          <p className="mt-1 text-[15px] text-muted-foreground">
            Browse, preview, and export your saved SuiteQL queries
          </p>
        </div>
        <Button onClick={() => setShowForm(!showForm)}>
          <Plus className="mr-2 h-4 w-4" />
          Save New Query
        </Button>
      </div>

      {/* Inline save form */}
      {showForm && (
        <InlineSaveForm onClose={() => setShowForm(false)} />
      )}

      {/* Content */}
      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-[160px] rounded-xl" />
          ))}
        </div>
      ) : !queries?.length ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card py-16">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
            <Database className="h-6 w-6 text-muted-foreground" />
          </div>
          <p className="mt-4 text-[15px] font-medium text-foreground">
            No saved queries yet
          </p>
          <p className="mt-1 mb-5 text-[13px] text-muted-foreground">
            Save a SuiteQL query from chat or click &quot;Save New Query&quot; above.
          </p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {queries.map((query) => (
            <div
              key={query.id}
              className="group relative cursor-pointer rounded-xl border bg-card p-5 shadow-soft transition-all duration-200 hover:border-primary hover:shadow-soft-md"
              onClick={() => setSelectedQuery(query)}
            >
              <div className="flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                  <TableIcon className="h-5 w-5 text-primary transition-transform group-hover:scale-110" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[15px] font-semibold text-foreground">
                    {query.name}
                  </p>
                  <p className="mt-0.5 line-clamp-2 text-[13px] text-muted-foreground">
                    {query.description || "No description"}
                  </p>
                </div>
              </div>

              {/* Footer */}
              <div className="mt-4 flex items-center justify-between border-t pt-3">
                <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                  <Calendar className="h-3.5 w-3.5" />
                  {new Date(query.created_at).toLocaleDateString()}
                </div>

                {/* Delete button */}
                {confirmDeleteId === query.id ? (
                  <div
                    className="flex items-center gap-2"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Button
                      variant="destructive"
                      size="sm"
                      className="h-7 text-[12px]"
                      onClick={() => handleDelete(query.id)}
                      disabled={deleteMutation.isPending}
                    >
                      Confirm
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-[12px]"
                      onClick={() => setConfirmDeleteId(null)}
                    >
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100"
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmDeleteId(query.id);
                    }}
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Preview modal */}
      {selectedQuery && (
        <QueryPreviewModal
          query={selectedQuery}
          onClose={() => setSelectedQuery(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline save form
// ---------------------------------------------------------------------------

function InlineSaveForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [queryText, setQueryText] = useState("");
  const createMutation = useCreateSavedQuery();
  const { toast } = useToast();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !queryText.trim()) return;
    try {
      await createMutation.mutateAsync({
        name: name.trim(),
        description: description.trim() || undefined,
        query_text: queryText.trim(),
      });
      toast({ title: "Query saved" });
      onClose();
    } catch (err) {
      toast({
        title: "Failed to save query",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-xl border bg-card p-5 shadow-soft space-y-3"
    >
      <div className="flex items-center justify-between">
        <p className="text-[13px] font-semibold text-foreground">
          Save a New Query
        </p>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <input
        type="text"
        placeholder="Query name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        required
      />

      <input
        type="text"
        placeholder="Description (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      />

      <textarea
        placeholder="SELECT id, tranid FROM transaction WHERE type = 'SalesOrd'"
        value={queryText}
        onChange={(e) => setQueryText(e.target.value)}
        rows={4}
        className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] font-mono text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring resize-none scrollbar-thin"
        required
      />

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={!name.trim() || !queryText.trim() || createMutation.isPending}
        >
          {createMutation.isPending && (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          )}
          Save Query
        </Button>
      </div>
    </form>
  );
}
