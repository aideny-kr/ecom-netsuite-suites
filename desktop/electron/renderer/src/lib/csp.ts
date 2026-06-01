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
 * INLINE-SCRIPT HYDRATION (resolved — post-build hashing): `next build` emits
 * ~5 INLINE bootstrap <script> tags (the app-router RSC Flight payload) that
 * hydration requires. Under PACKAGED_CSP's strict `script-src 'self'` (no
 * `'unsafe-inline'`) those would be blocked — so PACKAGED_CSP below is the BASE
 * `script-src`, and the post-build step `scripts/inject-csp.mjs` (chained into
 * the renderer `build`) recomputes each inline script's per-build sha256 and
 * appends `'sha256-…'` to `script-src` in every out/*.html. The policy stays
 * strict (NEVER `'unsafe-inline'`/`'unsafe-eval'` for scripts); the hashes are
 * recomputed each build (the inline bytes embed the buildId + chunk hashes, so
 * they can't be pinned here). `buildId` is pinned in next.config.mjs for
 * reproducibility. main.ts wires a `console-message` CSP-violation listener as
 * the key-free hydration gate; the operator confirms the live render via
 * `npm start` (see desktop/SMOKE-DEFERRAL-RICH-PIPE.md). The key-free DONE proof
 * (renderer vitest tests) does not exercise Next hydration and is unaffected.
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
