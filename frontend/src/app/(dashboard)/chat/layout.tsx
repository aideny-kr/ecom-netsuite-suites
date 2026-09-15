"use client";

export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return <div className="chat-container h-full min-h-0">{children}</div>;
}
