import { fireEvent, render, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DeleteReportDialog } from "../delete-report-dialog";

const baseReport = { id: "r-1", title: "Q2 Review", version: 3 };
const idleMutation = { mutate: vi.fn(), isPending: false, error: null } as never;

it("renders the plural body copy with the report title and version count", () => {
  const { getByText, getByRole } = render(
    <DeleteReportDialog
      report={baseReport}
      open
      onOpenChange={vi.fn()}
      onDeleted={vi.fn()}
      deleteMutation={idleMutation}
    />,
  );
  expect(getByRole("heading", { name: "Delete this report?" })).toBeInTheDocument();
  expect(
    getByText(
      "Q2 Review and its 3 saved versions will be permanently removed for everyone in this workspace, and its auto-refresh will stop. This can't be undone.",
    ),
  ).toBeInTheDocument();
});

it("renders the singular body copy when version === 1", () => {
  const { getByText } = render(
    <DeleteReportDialog
      report={{ ...baseReport, version: 1 }}
      open
      onOpenChange={vi.fn()}
      onDeleted={vi.fn()}
      deleteMutation={idleMutation}
    />,
  );
  expect(
    getByText(
      "Q2 Review will be permanently removed for everyone in this workspace, and its auto-refresh will stop. This can't be undone.",
    ),
  ).toBeInTheDocument();
});

it("Cancel closes the dialog without calling delete", () => {
  const onOpenChange = vi.fn();
  const deleteMutate = vi.fn();
  const { getByRole } = render(
    <DeleteReportDialog
      report={baseReport}
      open
      onOpenChange={onOpenChange}
      onDeleted={vi.fn()}
      deleteMutation={{ mutate: deleteMutate, isPending: false, error: null } as never}
    />,
  );
  fireEvent.click(getByRole("button", { name: "Cancel" }));
  expect(deleteMutate).not.toHaveBeenCalled();
  expect(onOpenChange).toHaveBeenCalledWith(false);
});

it("Delete report fires the mutation and calls onDeleted on success", async () => {
  const onDeleted = vi.fn();
  const deleteMutate = vi.fn((_vars, opts) => opts?.onSuccess?.());
  const { getByRole } = render(
    <DeleteReportDialog
      report={baseReport}
      open
      onOpenChange={vi.fn()}
      onDeleted={onDeleted}
      deleteMutation={{ mutate: deleteMutate, isPending: false, error: null } as never}
    />,
  );
  fireEvent.click(getByRole("button", { name: "Delete report" }));
  expect(deleteMutate).toHaveBeenCalled();
  await waitFor(() => expect(onDeleted).toHaveBeenCalled());
});

it("disables both buttons while the mutation is pending", () => {
  const { getByRole } = render(
    <DeleteReportDialog
      report={baseReport}
      open
      onOpenChange={vi.fn()}
      onDeleted={vi.fn()}
      deleteMutation={{ mutate: vi.fn(), isPending: true, error: null } as never}
    />,
  );
  expect(getByRole("button", { name: "Cancel" })).toBeDisabled();
  expect(getByRole("button", { name: "Delete report" })).toBeDisabled();
});

it("surfaces the mutation error inline in the dialog", () => {
  const { getByText } = render(
    <DeleteReportDialog
      report={baseReport}
      open
      onOpenChange={vi.fn()}
      onDeleted={vi.fn()}
      deleteMutation={
        { mutate: vi.fn(), isPending: false, error: new Error("Only the report's creator or a workspace admin can delete this report") } as never
      }
    />,
  );
  expect(
    getByText("Only the report's creator or a workspace admin can delete this report"),
  ).toBeInTheDocument();
});
