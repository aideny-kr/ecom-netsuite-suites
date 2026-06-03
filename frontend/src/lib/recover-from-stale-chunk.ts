/**
 * Stale-chunk recovery helpers.
 *
 * After a frontend deploy, an open tab runs the old bundle. When it lazily
 * imports a code-split chunk that no longer exists on the new container, the
 * browser throws a `ChunkLoadError` (or a dynamic-import failure). The fix is
 * a single full reload, which pulls the new bundle.
 *
 * RELOAD-LOOP SAFETY: `reloadOnceForStaleChunk` is guarded by a sessionStorage
 * timestamp. If we reloaded within the last `RELOAD_GUARD_MS`, we do NOT reload
 * again — an infinite reload loop is strictly worse than the original bug.
 */

import { BUILD_ID } from "@/lib/build-id";

const RELOAD_GUARD_KEY = "__sb_chunk_reload_at";
const RELOAD_GUARD_MS = 10_000;

const STALE_CHUNK_PATTERNS: RegExp[] = [
  /Loading chunk [\w]+ failed/i,
  /Failed to fetch dynamically imported module/i,
  /Importing a module script failed/i,
];

/**
 * True when `err` looks like a stale code-split chunk / dynamic-import failure.
 * Matches by error name (`ChunkLoadError`) or by message pattern.
 */
export function isStaleChunkError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  if (err.name === "ChunkLoadError") return true;
  const message = err.message || "";
  return STALE_CHUNK_PATTERNS.some((re) => re.test(message));
}

/**
 * Reload the page to pull the new bundle — but only once per guard window.
 *
 * Uses sessionStorage so the guard survives the reload itself: if we already
 * reloaded < 10s ago, we bail (the new bundle clearly didn't fix it, or two
 * errors fired in the same tick) rather than loop forever.
 *
 * @returns `true` iff a reload was actually triggered. Returns `false` when:
 *   - `BUILD_ID === "dev"` (local dev: HMR dynamic-import errors must NOT
 *     reload the dev's page),
 *   - the 10s loop-guard blocks a repeat reload, or
 *   - `window` / `sessionStorage` is unavailable (SSR / private mode).
 *
 * Callers (error boundaries) use the return value to decide whether to render
 * a visible fallback: when no reload is in flight, never leave a blank page.
 */
export function reloadOnceForStaleChunk(): boolean {
  // Local dev: a missing chunk is almost always an HMR hiccup, not a deploy
  // skew. Never auto-reload the dev's page — fall through to the fallback.
  if (BUILD_ID === "dev") return false;

  if (typeof window === "undefined") return false;

  try {
    const now = Date.now();
    const last = Number(window.sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
    if (last && now - last < RELOAD_GUARD_MS) {
      // We reloaded very recently — do NOT loop.
      return false;
    }
    window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(now));
  } catch {
    // sessionStorage unavailable (private mode / SSR). Reloading without a
    // guard risks a loop, so be conservative and skip the auto-reload.
    return false;
  }

  window.location.reload();
  return true;
}
