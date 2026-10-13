import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { FileEditPanel } from "../file-edit-panel";

const mock = vi.hoisted(() => ({ propose: vi.fn(), permission: true }));
vi.mock("@/hooks/use-workspace", () => ({
  useProposeFileEdit: () => ({ mutateAsync: mock.propose, isPending: false }),
}));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => mock.permission }) }));
vi.mock("../code-viewer", () => ({ CodeViewer: ({ content }: { content: string }) => <pre>{content}</pre> }));
const file = { id: "f1", path: "src/a.js", file_name: "a.js", content: "old\n", sha256_hash: "a".repeat(64), can_edit: true, truncated: false, total_lines: 1, mime_type: null };
const props = () => ({ workspaceId: "w1", file, drafts: { current: {} }, onReview: vi.fn() });
beforeEach(() => { mock.propose.mockReset(); mock.permission = true; });

describe("manual workspace drafts", () => {
  it("proposes the exact file revision and opens existing diff review", async () => {
    const p = props();mock.propose.mockResolvedValue({ changeset_id: "cs1" });render(<FileEditPanel {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit code" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Edit src/a.js" }), { target: { value: "new\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft for review" }));
    await waitFor(() => expect(p.onReview).toHaveBeenCalledWith("cs1"));
    expect(mock.propose).toHaveBeenCalledWith({ workspaceId: "w1", fileId: "f1", content: "new\n", baseline_sha256: file.sha256_hash });
  });
  it("preserves unsaved edits across file switching and query remounts", () => {
    const p=props();const view=render(<FileEditPanel {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit code" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "draft" } });
    view.unmount();render(<FileEditPanel {...p} />);
    expect(screen.getByRole("textbox")).toHaveValue("draft");
  });
  it("does not redirect a newly selected project after an old save resolves", async () => {
    let finish!: (value: { changeset_id: string }) => void;
    mock.propose.mockImplementation(() => new Promise(resolve => { finish=resolve; }));
    const p=props();const view=render(<FileEditPanel {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit code" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft for review" }));
    view.rerender(<FileEditPanel {...p} workspaceId="w2" />);finish({ changeset_id: "old-cs" });
    await waitFor(()=>expect(screen.getByRole("button", { name: "Edit code" })).toBeEnabled());
    expect(p.onReview).not.toHaveBeenCalled();
  });
  it("retains draft on a stale-revision error", async () => {
    mock.propose.mockRejectedValue(new Error("File changed; reload"));render(<FileEditPanel {...props()} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit code" }));fireEvent.change(screen.getByRole("textbox"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft for review" }));
    await screen.findByRole("alert");expect(screen.getByRole("textbox")).toHaveValue("draft");
  });
  it("cannot edit partial files or without manage permission", () => {
    const p=props();const view=render(<FileEditPanel {...p} file={{...file,truncated:true}} />);
    expect(screen.queryByRole("button",{name:"Edit code"})).not.toBeInTheDocument();
    view.rerender(<FileEditPanel {...p} file={{...file,can_edit:false}} />);
    expect(screen.queryByRole("button",{name:"Edit code"})).not.toBeInTheDocument();
  });
  it("keeps newer typing after a pending save's editor was remounted", async () => {
    let finish!: (value: { changeset_id: string }) => void;
    mock.propose.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
    const p=props();const view=render(<FileEditPanel {...p} />);
    fireEvent.click(screen.getByRole("button",{name:"Edit code"}));
    fireEvent.change(screen.getByRole("textbox"),{target:{value:"saved snapshot"}});
    fireEvent.click(screen.getByRole("button",{name:"Save draft for review"}));
    view.unmount();render(<FileEditPanel {...p} />);
    fireEvent.change(screen.getByRole("textbox"),{target:{value:"newer unsaved typing"}});
    finish({changeset_id:"older-save"});
    await waitFor(()=>expect(p.drafts.current["w1:f1"]).toBeDefined());
    await new Promise(resolve=>setTimeout(resolve,0));
    expect(p.drafts.current["w1:f1"].content).toBe("newer unsaved typing");
  });
});
