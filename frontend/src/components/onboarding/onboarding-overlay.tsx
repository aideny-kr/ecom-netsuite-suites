"use client";

import { useEffect, useState, useCallback } from "react";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useOnboardingChat } from "@/hooks/use-onboarding-chat";
import { Sparkles } from "lucide-react";

interface OnboardingOverlayProps {
  onComplete: () => void;
}

export function OnboardingOverlay({ onComplete }: OnboardingOverlayProps) {
  const {
    messages,
    sendMessage,
    startSession,
    isLoading,
    isStarting,
    isComplete,
  } = useOnboardingChat();
  const [isExiting, setIsExiting] = useState(false);
  const [currentPhase, setCurrentPhase] = useState(0);

  useEffect(() => {
    startSession();
  }, [startSession]);

  // Detect current phase from messages
  useEffect(() => {
    const allContent = messages.map((m) => m.content).join(" ").toLowerCase();
    if (allContent.includes("customize") || allContent.includes("chart of accounts") || allContent.includes("subsidiaries")) {
      setCurrentPhase(2);
    } else if (allContent.includes("netsuite") || allContent.includes("account id")) {
      setCurrentPhase(1);
    }
  }, [messages]);

  // Handle completion
  useEffect(() => {
    if (isComplete) {
      const timer = setTimeout(() => {
        setIsExiting(true);
        setTimeout(onComplete, 500);
      }, 2000);
      return () => clearTimeout(timer);
    }
  }, [isComplete, onComplete]);

  const handleSkip = useCallback(() => {
    localStorage.setItem("onboarding_skipped", "true");
    setIsExiting(true);
    setTimeout(onComplete, 500);
  }, [onComplete]);

  const phases = ["Business", "NetSuite", "Customize"];

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center transition-all duration-500 ${
        isExiting
          ? "opacity-0 scale-105"
          : "opacity-100 scale-100 animate-in fade-in zoom-in-95 duration-500"
      }`}
      style={{
        background:
          "linear-gradient(135deg, hsl(250 40% 15%) 0%, hsl(230 35% 20%) 30%, hsl(260 30% 18%) 60%, hsl(240 35% 12%) 100%)",
      }}
    >
      {/* Subtle floating particles */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute left-1/4 top-1/4 h-64 w-64 rounded-full bg-purple-500/5 blur-3xl" />
        <div className="absolute right-1/3 bottom-1/3 h-96 w-96 rounded-full bg-blue-500/5 blur-3xl" />
        <div className="absolute left-1/2 top-1/2 h-48 w-48 -translate-x-1/2 -translate-y-1/2 rounded-full bg-indigo-500/5 blur-3xl" />
      </div>

      <div className="relative flex h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-white/10 bg-background/95 shadow-2xl backdrop-blur-xl">
        {/* Header */}
        <div className="flex flex-col items-center gap-4 border-b px-6 pb-5 pt-8">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10">
            <Sparkles className="h-6 w-6 text-primary" />
          </div>
          <div className="text-center">
            <h1 className="text-xl font-semibold tracking-tight">
              Welcome to your workspace
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Let&apos;s get you set up in just a few minutes
            </p>
          </div>

          {/* Progress indicator */}
          <div className="flex items-center gap-2">
            {phases.map((phase, idx) => (
              <div key={phase} className="flex items-center gap-2">
                <div className="flex items-center gap-1.5">
                  <div
                    className={`h-2 w-2 rounded-full transition-colors duration-300 ${
                      idx <= currentPhase
                        ? "bg-primary"
                        : "bg-muted-foreground/20"
                    }`}
                  />
                  <span
                    className={`text-xs font-medium transition-colors duration-300 ${
                      idx <= currentPhase
                        ? "text-foreground"
                        : "text-muted-foreground/50"
                    }`}
                  >
                    {phase}
                  </span>
                </div>
                {idx < phases.length - 1 && (
                  <div
                    className={`h-px w-6 transition-colors duration-300 ${
                      idx < currentPhase
                        ? "bg-primary/40"
                        : "bg-muted-foreground/10"
                    }`}
                  />
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-hidden">
          {isStarting ? (
            <div className="flex h-full items-center justify-center">
              <div className="flex flex-col items-center gap-3">
                <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                <span className="text-sm text-muted-foreground">
                  Preparing your onboarding...
                </span>
              </div>
            </div>
          ) : (
            <MessageList
              messages={messages}
              isLoading={false}
              isWaitingForReply={isLoading}
            />
          )}
        </div>

        {/* Input */}
        {!isComplete && !isStarting && (
          <ChatInput onSend={sendMessage} isLoading={isLoading} />
        )}

        {/* Completion message */}
        {isComplete && (
          <div className="border-t px-6 py-6 text-center">
            <div className="flex items-center justify-center gap-2 text-primary">
              <Sparkles className="h-5 w-5" />
              <span className="text-sm font-medium">
                You&apos;re all set! Redirecting to your dashboard...
              </span>
            </div>
          </div>
        )}

        {/* Skip button */}
        {!isComplete && !isStarting && (
          <div className="px-6 pb-4 text-center">
            <button
              onClick={handleSkip}
              className="text-xs text-muted-foreground/60 transition-colors hover:text-muted-foreground"
            >
              Set up later
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
