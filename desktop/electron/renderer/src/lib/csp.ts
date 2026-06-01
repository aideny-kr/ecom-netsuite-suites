/**
 * Content-Security-Policy for the desktop renderer.
 *
 * PACKAGED_CSP is the strict policy for the static export Electron loads via
 * file:// — no remote origins, no `unsafe-eval`, and `script-src 'self'` (no
 * inline scripts), mirroring the B0 renderer.html contract verified in the B0
 * security review. `style-src` permits `'unsafe-inline'` because Tailwind/Next
 * inject a <style> block — inline STYLES are allowed; inline SCRIPTS are not.
 *
 * DEV_CSP loosens script/style to `'unsafe-inline' 'unsafe-eval'` and allows the
 * dev-server websocket so `next dev` HMR works; it is NEVER shipped (layout
 * selects PACKAGED_CSP whenever NODE_ENV === "production", i.e. the export).
 *
 * KNOWN BLOCKER (operator-deferred live render): `next build` of this app emits
 * ~5 INLINE bootstrap <script> tags (the app-router hydration chunks). Under
 * PACKAGED_CSP's `script-src 'self'` (no `'unsafe-inline'`) those are blocked, so
 * the static HTML renders but hydration/interactivity (runAgentStream) does NOT
 * run. This MUST be reconciled before the live render works — options: add the
 * per-build sha256 hashes of those inline scripts to `script-src` (a post-build
 * step), or ship the interactivity as a single external-script island. The
 * policy intentionally stays strict (never `'unsafe-inline'`/`'unsafe-eval'` for
 * scripts) — confirmed during slice 1, tracked in
 * desktop/SMOKE-DEFERRAL-RICH-PIPE.md. The key-free DONE proof (renderer vitest
 * tests) does not exercise Next hydration and is unaffected.
 */
export const PACKAGED_CSP =
  "default-src 'self'; " +
  "script-src 'self'; " +
  "style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; " +
  "font-src 'self' data:; " +
  "connect-src 'self'; " +
  "object-src 'none'; " +
  "base-uri 'none'; " +
  "frame-ancestors 'none';";

export const DEV_CSP =
  "default-src 'self'; " +
  "script-src 'self' 'unsafe-inline' 'unsafe-eval'; " +
  "style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; " +
  "font-src 'self' data:; " +
  "connect-src 'self' ws: http://localhost:*; " +
  "object-src 'none';";

export function rendererCsp(isProduction: boolean): string {
  return isProduction ? PACKAGED_CSP : DEV_CSP;
}
