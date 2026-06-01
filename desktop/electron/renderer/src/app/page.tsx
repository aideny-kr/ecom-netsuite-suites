"use client";

// Chat page. The interactive view consumes window.suiteStudio.runAgentStream and
// renders streamed text + the reused data_table card (rich-pipe slice 1, C3).
import { ChatView } from "@/components/chat/chat-view";

export default function Page() {
  return <ChatView />;
}
