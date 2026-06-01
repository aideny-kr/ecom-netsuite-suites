import type { Metadata } from "next";
import "./globals.css";
import { rendererCsp } from "@/lib/csp";

export const metadata: Metadata = {
  title: "Suite Studio Desktop",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Production build (the static export Electron ships) gets the strict CSP;
  // `next dev` gets the looser dev CSP so HMR works. See @/lib/csp.
  //
  // In production this emits PACKAGED_CSP with a bare `script-src 'self'` — it is
  // the BASE the post-build step (scripts/inject-csp.mjs) rewrites, appending the
  // per-build sha256 hashes of Next's inline RSC-bootstrap scripts so hydration
  // runs under the strict policy. The un-hashed base never ships; the injector
  // overwrites this <meta> in out/*.html before electron-builder copies it.
  const csp = rendererCsp(process.env.NODE_ENV === "production");
  return (
    <html lang="en">
      <head>
        <meta charSet="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta httpEquiv="X-Content-Type-Options" content="nosniff" />
        <meta httpEquiv="Content-Security-Policy" content={csp} />
      </head>
      <body>{children}</body>
    </html>
  );
}
