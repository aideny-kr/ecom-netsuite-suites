"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useToast } from "@/hooks/use-toast";

export function usePullFile() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: ({
      fileId,
      workspaceId,
    }: {
      fileId: string;
      workspaceId: string;
    }) =>
      apiClient.post(`/api/v1/netsuite/scripts/${fileId}/pull`, {
        workspace_id: workspaceId,
      }),
    onSuccess: (data: unknown, variables) => {
      const result = data as Record<string, unknown> | null;
      queryClient.invalidateQueries({
        queryKey: ["workspace-files", variables.workspaceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["file-content", variables.workspaceId, variables.fileId],
      });
      queryClient.invalidateQueries({
        queryKey: ["netsuite-api-logs"],
      });
      toast({
        title: "Pulled from NetSuite",
        description: `${(result?.file_name as string) || "File"} updated successfully.`,
      });
    },
    onError: (err: Error) => {
      const detail = err.message || "Pull failed";
      if (detail.includes("not linked to NetSuite")) {
        toast({
          variant: "destructive",
          title: "File not linked",
          description:
            "This file hasn't been linked to NetSuite yet. Try running Sync Scripts first.",
        });
      } else {
        toast({
          variant: "destructive",
          title: "Pull failed",
          description: detail,
        });
      }
    },
  });
}

export function usePushFile() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: ({
      fileId,
      workspaceId,
    }: {
      fileId: string;
      workspaceId: string;
    }) =>
      apiClient.post(`/api/v1/netsuite/scripts/${fileId}/push`, {
        workspace_id: workspaceId,
      }),
    onSuccess: (data: unknown) => {
      const result = data as Record<string, unknown> | null;
      queryClient.invalidateQueries({
        queryKey: ["netsuite-api-logs"],
      });
      toast({
        title: "Pushed to NetSuite",
        description: `${(result?.file_name as string) || "File"} pushed successfully.`,
      });
    },
    onError: (err: Error) => {
      const detail = err.message || "Push failed";
      toast({
        variant: "destructive",
        title: "Push failed",
        description: detail,
      });
    },
  });
}
