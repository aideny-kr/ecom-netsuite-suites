/**
 * Next.js config for the desktop renderer.
 *
 * `output: "export"` emits a fully static site to `./out` that Electron loads
 * via `loadFile` (packaged) — no Node server in the .app. `assetPrefix: "./"`
 * keeps asset URLs relative so they resolve under the `file://` origin.
 * `images.unoptimized` is required for static export.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  assetPrefix: "./",
  images: { unoptimized: true },
  reactStrictMode: true,
  // Pin the buildId so the inline RSC-bootstrap bytes (which embed it) are
  // stable across rebuilds of unchanged code. This keeps the post-build CSP
  // hashes (scripts/inject-csp.mjs) reviewable and reproducible. Chunk
  // content-hashes still change when code changes — the post-build step
  // recomputes every build, so the CSP stays correct either way.
  generateBuildId: () => "suite-studio",
};

export default nextConfig;
