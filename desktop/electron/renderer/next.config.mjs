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
};

export default nextConfig;
