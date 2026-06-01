import type { Metadata } from "next";
import "./globals.css";
import { rendererCsp } from "@/lib/csp";

export const metadata: Metadata = {
  title: "Suite Studio Desktop",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Production build (the static export Electron ships) gets the strict CSP;
  // `next dev` gets the looser dev CSP so HMR works. See @/lib/csp.
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
