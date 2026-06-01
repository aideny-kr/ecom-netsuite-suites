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
 * NOTE (operator-deferred live render): Next's static export may emit an inline
 * bootstrap script for hydration; reconciling that with `script-src 'self'`
 * (nonce/hash or a single external island) is finalized during the deferred live
 * render smoke — see desktop/SMOKE-DEFERRAL-RICH-PIPE.md. The policy stays
 * strict (never `'unsafe-inline'`/`'unsafe-eval'` for scripts).
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
