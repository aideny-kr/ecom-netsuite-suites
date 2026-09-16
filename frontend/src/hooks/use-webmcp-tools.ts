"use client";

import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useAuth } from "@/providers/auth-provider";
import { getWebMcpContext, runInWebMcpSession, type WebMcpTool } from "@/lib/webmcp";

export type WebMcpAction = Omit<WebMcpTool, "execute"> & {
  execute: (input: unknown, assertCurrent?: () => void) => unknown | Promise<unknown>;
};

/** Async actions must re-read committed UI state after each await. A closure
 * over a render's values is insufficient when selection changes mid-request. */
export function useWebMcpState<T>(state: T): () => T {
  const current = useRef(state);
  useLayoutEffect(() => { current.current = state; });
  return useCallback(() => current.current, []);
}

/** Route tools call live UI handlers and expire on route/selection/auth change. */
export function useWebMcpTools(scope: string, actions: WebMcpAction[]) {
  const { user, isLoading } = useAuth();
  const current = useRef({ user: isLoading ? null : user, actions });
  useLayoutEffect(() => { current.current = { user: isLoading ? null : user, actions }; });
  const names = actions.map((tool) => tool.name).join(",");
  useEffect(() => {
    const context = getWebMcpContext();
    if (!context || isLoading || !user) return;
    const controller = new AbortController();
    for (const action of current.current.actions) {
      const tool: WebMcpTool = {
        ...action,
        execute: async (input) => {
          try { return await runInWebMcpSession(() => current.current.user, controller.signal, (assertCurrent) => {
          const live = current.current.actions.find((item) => item.name === action.name);
          if (!live) throw new Error("Tool is no longer available on this page.");
          return live.execute(input, assertCurrent);
          }); } catch (error) {
            // Chrome normalizes thrown exceptions to UnknownError. Return bounded
            // diagnostics after the session guard has sanitized API failures.
            return JSON.stringify({ error: { message: error instanceof Error ? error.message.slice(0, 250) : "Tool failed." },
              retry_hint: "Read current state. Retrying a chat submission must reuse the same session_id and request_id." });
          }
        },
      };
      try {
        Promise.resolve(context.registerTool(tool, { signal: controller.signal })).catch(() => {
          if (!controller.signal.aborted) console.warn(`WebMCP could not register ${tool.name}.`);
        });
      } catch { console.warn(`WebMCP could not register ${tool.name}.`); }
    }
    return () => controller.abort();
  // Handlers are read through current; only identity and tool scope own lifetime.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, names, isLoading, user?.id, user?.tenant_id]);
}
