"use client";

import { useState } from "react";
import { useSavedQueries } from "@/hooks/use-saved-queries";
import type { SavedQueryResponse } from "@/types/analytics";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Loader2, Table as TableIcon, Database } from "lucide-react";
import { QueryPreviewModal } from "@/components/analytics/QueryPreviewModal";

export function SavedQueriesList({ enabled }: { enabled: boolean }) {
  const { data: queries, isLoading } = useSavedQueries(enabled);

  const [selectedQuery, setSelectedQuery] = useState<SavedQueryResponse | null>(null);

  if (isLoading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    );
  }

  if (!queries?.length) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
          <Database className="h-6 w-6 text-muted-foreground" />
        </div>
        <p className="mt-3 text-[13px] text-muted-foreground">
          No saved queries yet.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {queries.map((query) => (
          <Card
            key={query.id}
            className="group cursor-pointer rounded-xl border bg-card p-0 shadow-soft transition-all hover:border-primary hover:shadow-soft-md"
            onClick={() => setSelectedQuery(query)}
          >
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base text-foreground">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                  <TableIcon className="h-4 w-4 text-primary group-hover:scale-110 transition-transform" />
                </div>
                <span className="truncate">{query.name}</span>
              </CardTitle>
              <CardDescription className="line-clamp-2 text-[13px] text-muted-foreground">
                {query.description || "No description provided."}
              </CardDescription>
            </CardHeader>
          </Card>
        ))}
      </div>

      {selectedQuery && (
        <QueryPreviewModal
          query={selectedQuery}
          onClose={() => setSelectedQuery(null)}
        />
      )}
    </>
  );
}
