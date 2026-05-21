---
description: Next.js 14 + React Query + Tailwind + react-resizable-panels v4 rules. Loads when editing frontend.
paths:
  - frontend/src/**
  - frontend/**/*.tsx
  - frontend/**/*.ts
---

# Frontend rules

1. **`react-resizable-panels` v4** — import `Group as PanelGroup`, `Separator as PanelResizeHandle`. Use `orientation` (not `direction`).
2. **Always use `apiClient`** — never raw `fetch()`. Access token is in localStorage; `apiClient` handles Auth header.
3. **`"use client"` on any file using hooks.**
4. **`normalizeStreamMessage` must preserve `structured_output`** — when adding structured types, the SSE terminal `message` event's `structured_output` field MUST be copied in `frontend/src/lib/chat-stream.ts::normalizeStreamMessage()`.
5. **One Next.js dev server per project** — run `npm run dev` from main checkout, not a stale worktree. Check `ps aux | grep next-dev` if hot reload misbehaves.
6. **Next.js route groups don't appear in URLs** — files in `app/(dashboard)/foo` serve at `/foo`, not `/dashboard/foo`. Check `NAV_ITEMS` for the real URL.
7. **SSE must use fetch-streaming, never `EventSource`** — `EventSource` can't send `Authorization` header. Use `apiClient.streamGet` like chat.

## React Query hook template

```typescript
"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export function useResource(id: string | null) {
  return useQuery<Resource>({
    queryKey: ["resources", id],
    queryFn: () => apiClient.get<Resource>(`/api/v1/resources/${id}`),
    enabled: !!id,
  });
}

export function useCreateResource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateResourcePayload) =>
      apiClient.post<Resource>("/api/v1/resources", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}
```

**Rules:**
- Always `"use client"` at top of files using hooks
- Always use `apiClient` from `@/lib/api-client` — never raw `fetch()`
- Query keys: `["entity"]` for lists, `["entity", id]` for single, `["entity", id, "sub"]` for nested
- Mutations always invalidate parent query on success
- Use `enabled: !!id` for conditional queries

## Page component rules

- Icons from `lucide-react` only
- Spacing: `space-y-8` for page sections, `gap-4` for grids
- Text sizes: `text-2xl` for page titles, `text-[15px]` for body, `text-[13px]` for labels/captions
- Colors: `text-foreground` for primary, `text-muted-foreground` for secondary
- Cards: `rounded-xl border bg-card p-5 shadow-soft`
- Use `animate-fade-in` on page root
