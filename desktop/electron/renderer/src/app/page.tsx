"use client";

// Minimal chat page shell (scaffold). The interactive chat view that consumes
// window.suiteStudio.runAgentStream and renders the data_table card is wired in
// Task C3 (replaces this placeholder body).
export default function Page() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-4 p-6 animate-fade-in">
      <h1 className="text-2xl font-semibold text-foreground">Suite Studio Desktop</h1>
      <p className="text-[15px] text-muted-foreground">
        Ask a question to see rich results stream into the chat.
      </p>
    </main>
  );
}
