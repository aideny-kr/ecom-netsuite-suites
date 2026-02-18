"use client";

import { useEffect } from "react";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useOnboardingChat } from "@/hooks/use-onboarding-chat";
import { Sparkles } from "lucide-react";

const STEP_LABELS: Record<string, string> = {
  profile: "Business Profile",
  connection: "NetSuite Connection",
  policy: "Policy Setup",
  workspace: "Workspace Setup",
  first_success: "First Success",
};

interface ChatCopilotPanelProps {
  wizardStep: string;
}

export function ChatCopilotPanel({ wizardStep }: ChatCopilotPanelProps) {
  const {
    messages,
    sendMessage,
    startSession,
    isLoading,
    isStarting,
    setWizardStep,
  } = useOnboardingChat();

  useEffect(() => {
    startSession();
  }, [startSession]);

  useEffect(() => {
    setWizardStep(wizardStep);
  }, [wizardStep, setWizardStep]);

  return (
    <div className="flex h-full flex-col border-l bg-card">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
        </div>
        <div>
          <p className="text-sm font-medium">AI Assistant</p>
          <p className="text-[11px] text-muted-foreground">
            {STEP_LABELS[wizardStep] || "Onboarding"}
          </p>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-hidden">
        {isStarting ? (
          <div className="flex h-full items-center justify-center">
            <div className="flex flex-col items-center gap-3">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
              <span className="text-sm text-muted-foreground">Starting assistant...</span>
            </div>
          </div>
        ) : (
          <MessageList messages={messages} isLoading={false} isWaitingForReply={isLoading} />
        )}
      </div>

      {/* Input */}
      {!isStarting && <ChatInput onSend={sendMessage} isLoading={isLoading} />}
    </div>
  );
}
