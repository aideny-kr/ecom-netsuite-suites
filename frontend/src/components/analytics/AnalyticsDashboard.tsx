"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { SavedQueryCreatePayload, SavedQueryResponse } from "@/types/analytics";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Plus, X, Loader2 } from "lucide-react";
import { SavedQueriesList } from "@/components/analytics/SavedQueriesList";

export function AnalyticsDashboard({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const [showForm, setShowForm] = useState(false);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl bg-card max-h-[90vh] overflow-y-auto scrollbar-thin">
        <DialogHeader className="flex flex-row items-center justify-between gap-4">
          <div>
            <DialogTitle className="text-foreground">
              Saved SuiteQL Analytics
            </DialogTitle>
            <DialogDescription className="text-[13px] text-muted-foreground">
              Select a query to preview results or export to CSV.
            </DialogDescription>
          </div>
          {!showForm && (
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              onClick={() => setShowForm(true)}
            >
              <Plus className="mr-1.5 h-4 w-4" />
              Save New Query
            </Button>
          )}
        </DialogHeader>

        {showForm && (
          <SaveQueryForm onClose={() => setShowForm(false)} />
        )}

        <SavedQueriesList enabled={open} />
      </DialogContent>
    </Dialog>
  );
}

function SaveQueryForm({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [queryText, setQueryText] = useState("");

  const mutation = useMutation({
    mutationFn: (data: SavedQueryCreatePayload) =>
      apiClient.post<SavedQueryResponse>("/api/v1/skills", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["saved-queries"] });
      onClose();
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !queryText.trim()) return;
    mutation.mutate({
      name: name.trim(),
      description: description.trim() || undefined,
      query_text: queryText.trim(),
    });
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-xl border bg-muted/30 p-4 space-y-3"
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

      {mutation.isError && (
        <p className="text-[13px] text-destructive">
          {mutation.error instanceof Error
            ? mutation.error.message
            : "Failed to save query."}
        </p>
      )}

      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={onClose}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={!name.trim() || !queryText.trim() || mutation.isPending}
        >
          {mutation.isPending && (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          )}
          Save Query
        </Button>
      </div>
    </form>
  );
}
