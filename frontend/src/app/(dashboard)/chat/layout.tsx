"use client";

import { Space_Grotesk } from "next/font/google";

const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-headline",
  display: "swap",
});

export default function ChatLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div
      className={`${spaceGrotesk.variable} dark chat-container h-full min-h-0`}
    >
      {children}
    </div>
  );
}
