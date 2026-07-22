"use client";

import type { MouseEvent } from "react";
import type { UseMutationResult } from "@tanstack/react-query";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

export interface DeleteReportDialogReport {
  id: string;
  title: string;
  version: number;
}

interface DeleteReportDialogProps {
  report: DeleteReportDialogReport;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after the delete mutation succeeds — caller decides navigate vs. just let invalidation remove the row. */
  onDeleted: () => void;
  /** Caller owns the mutation (usually `useDeleteReport(report.id)`) — keeps this a plain shared component. */
  deleteMutation: Pick<UseMutationResult<void, Error, void>, "mutate" | "isPending" | "error">;
}

export function DeleteReportDialog({
  report,
  open,
  onOpenChange,
  onDeleted,
  deleteMutation,
}: DeleteReportDialogProps) {
  const { mutate, isPending, error } = deleteMutation;

  const body =
    report.version === 1
      ? `${report.title} will be permanently removed for everyone in this workspace, and its auto-refresh will stop. This can't be undone.`
      : `${report.title} and its ${report.version} saved versions will be permanently removed for everyone in this workspace, and its auto-refresh will stop. This can't be undone.`;

  function handleDelete(e: MouseEvent<HTMLButtonElement>) {
    e.preventDefault(); // keep the dialog open — AlertDialogAction would otherwise auto-close on click
    mutate(undefined, { onSuccess: onDeleted });
  }

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this report?</AlertDialogTitle>
          <AlertDialogDescription>{body}</AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p role="alert" className="text-[13px] text-destructive">
            {error.message}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={handleDelete}
            disabled={isPending}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            Delete report
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
