"use client";

import { useState, useCallback, useRef } from "react";
import { apiClient } from "@/lib/api-client";
import type { ChatMessage } from "@/lib/types";

interface OnboardingStartResponse {
  session_id: string;
  message: {
    id: string;
    role: string;
    content: string;
    created_at: string;
  };
}

export function useOnboardingChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [wizardStep, setWizardStep] = useState<string | null>(null);
  const startedRef = useRef(false);

  const startSession = useCallback(async () => {
    if (startedRef.current) return;
    startedRef.current = true;
    setIsStarting(true);
    try {
      const res = await apiClient.post<OnboardingStartResponse>(
        "/api/v1/onboarding/chat/start",
      );
      setSessionId(res.session_id);
      // Add the initial user message and assistant greeting
      const greeting: ChatMessage = {
        id: res.message.id,
        role: res.message.role as "assistant",
        content: res.message.content,
        tool_calls: null,
        citations: null,
        created_at: res.message.created_at,
      };
      setMessages([greeting]);
    } catch (err) {
      console.error("Failed to start onboarding chat:", err);
      startedRef.current = false;
    } finally {
      setIsStarting(false);
    }
  }, []);

  const sendMessage = useCallback(
    async (content: string) => {
      if (!sessionId || isLoading) return;
      setIsLoading(true);

      // Optimistically add user message
      const userMsg: ChatMessage = {
        id: `temp-${Date.now()}`,
        role: "user",
        content,
        tool_calls: null,
        citations: null,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg]);

      try {
        const url = wizardStep
          ? `/api/v1/chat/sessions/${sessionId}/messages?wizard_step=${wizardStep}`
          : `/api/v1/chat/sessions/${sessionId}/messages`;
        const reply = await apiClient.post<ChatMessage>(url, { content });
        setMessages((prev) => [...prev, reply]);

        // Check if onboarding is complete (profile was saved)
        if (
          reply.content.includes("Profile saved") ||
          reply.content.includes("onboarding is complete") ||
          reply.content.includes("Onboarding is complete") ||
          reply.content.includes("all set") ||
          (reply.tool_calls &&
            reply.tool_calls.some(
              (tc) => tc.tool === "save_onboarding_profile",
            ))
        ) {
          setIsComplete(true);
        }
      } catch (err) {
        console.error("Failed to send message:", err);
        // Remove optimistic message on error
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id));
      } finally {
        setIsLoading(false);
      }
    },
    [sessionId, isLoading, wizardStep],
  );

  return {
    messages,
    sendMessage,
    startSession,
    isLoading,
    isStarting,
    isComplete,
    sessionId,
    wizardStep,
    setWizardStep,
  };
}
