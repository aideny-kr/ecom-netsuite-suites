"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface FeedbackPayload {
  messageId: string;
  feedback: "helpful" | "not_helpful";
}

export function useChatFeedback() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ messageId, feedback }: FeedbackPayload) =>
      apiClient.patch(`/api/v1/chat/messages/${messageId}/feedback?feedback=${feedback}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat_sessions"] });
    },
  });
}
