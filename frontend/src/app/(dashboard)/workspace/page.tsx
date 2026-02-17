"use client";

import { useState } from "react";
import { Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { FileTree } from "@/components/workspace/file-tree";
import { CodeViewer } from "@/components/workspace/code-viewer";
import { DiffViewer } from "@/components/workspace/diff-viewer";
import { WorkspaceSelector } from "@/components/workspace/workspace-selector";
import { ChangesetPanel } from "@/components/workspace/changeset-panel";
import { ImportDialog } from "@/components/workspace/import-dialog";
import {
  useWorkspaces,
  useCreateWorkspace,
  useWorkspaceFiles,
  useFileContent,
  useSearchFiles,
} from "@/hooks/use-workspace";
import { useChangesets, useChangesetDiff } from "@/hooks/use-changesets";

export default function WorkspacePage() {
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(
    null,
  );
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [selectedFilePath, setSelectedFilePath] = useState<string>("");
  const [searchQuery, setSearchQuery] = useState("");
  const [viewingDiffId, setViewingDiffId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  const { data: workspaces = [] } = useWorkspaces();
  const createWs = useCreateWorkspace();
  const { data: fileTree = [] } = useWorkspaceFiles(selectedWorkspaceId);
  const { data: fileContent } = useFileContent(
    selectedWorkspaceId,
    selectedFileId,
  );
  const { data: searchResults } = useSearchFiles(
    selectedWorkspaceId,
    searchQuery,
  );
  const { data: changesets = [] } = useChangesets(selectedWorkspaceId);
  const { data: diffData } = useChangesetDiff(viewingDiffId);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    try {
      const ws = await createWs.mutateAsync({
        name: newName.trim(),
        description: newDesc.trim() || undefined,
      });
      setSelectedWorkspaceId(ws.id);
      setCreateOpen(false);
      setNewName("");
      setNewDesc("");
    } catch {
      // handled by mutation
    }
  };

  const handleFileSelect = (fileId: string, path: string) => {
    setSelectedFileId(fileId);
    setSelectedFilePath(path);
    setViewingDiffId(null);
  };

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-3 border-b px-4 py-2.5">
        <WorkspaceSelector
          workspaces={workspaces}
          selectedId={selectedWorkspaceId}
          onSelect={setSelectedWorkspaceId}
        />
        <Dialog open={createOpen} onOpenChange={setCreateOpen}>
          <DialogTrigger asChild>
            <Button size="sm" variant="outline" className="h-8 text-[12px]">
              <Plus className="mr-1.5 h-3 w-3" />
              New
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Create Workspace</DialogTitle>
            </DialogHeader>
            <div className="space-y-3">
              <div>
                <Label className="text-[13px]">Name</Label>
                <Input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="My SDF Project"
                  className="mt-1"
                />
              </div>
              <div>
                <Label className="text-[13px]">Description</Label>
                <Input
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="Optional description"
                  className="mt-1"
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setCreateOpen(false)}>
                  Cancel
                </Button>
                <Button onClick={handleCreate} disabled={createWs.isPending}>
                  {createWs.isPending ? "Creating..." : "Create"}
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
        {selectedWorkspaceId && (
          <ImportDialog workspaceId={selectedWorkspaceId} />
        )}
        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search files..."
              className="h-8 w-[200px] pl-8 text-[12px]"
            />
          </div>
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: File Tree */}
        <div className="w-[260px] shrink-0 overflow-auto border-r bg-muted/20 p-2 scrollbar-thin">
          {selectedWorkspaceId ? (
            searchQuery && searchResults ? (
              <div className="space-y-1">
                <p className="px-2 text-[11px] font-medium text-muted-foreground">
                  {searchResults.length} result(s)
                </p>
                {searchResults.map((r) => (
                  <button
                    key={`${r.file_id}-${r.line_number}`}
                    onClick={() => handleFileSelect(r.file_id, r.path)}
                    className="block w-full rounded-md px-2 py-1 text-left hover:bg-accent"
                  >
                    <p className="truncate text-[12px] font-medium">
                      {r.path}
                    </p>
                    <p className="truncate text-[11px] text-muted-foreground">
                      L{r.line_number}: {r.snippet}
                    </p>
                  </button>
                ))}
              </div>
            ) : (
              <FileTree
                nodes={fileTree}
                onFileSelect={handleFileSelect}
                selectedFileId={selectedFileId}
              />
            )
          ) : (
            <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
              Select a workspace
            </div>
          )}
        </div>

        {/* Center: Code/Diff Viewer */}
        <div className="flex-1 overflow-hidden">
          {viewingDiffId && diffData ? (
            <div className="flex h-full flex-col">
              <div className="border-b px-4 py-2">
                <p className="text-[13px] font-medium">{diffData.title}</p>
              </div>
              <div className="flex-1">
                {diffData.files[0] && (
                  <DiffViewer
                    original={diffData.files[0].original_content}
                    modified={diffData.files[0].modified_content}
                    filePath={diffData.files[0].file_path}
                  />
                )}
              </div>
            </div>
          ) : fileContent ? (
            <div className="flex h-full flex-col">
              <div className="border-b px-4 py-2">
                <p className="text-[13px] font-medium font-mono">
                  {selectedFilePath}
                </p>
                {fileContent.truncated && (
                  <p className="text-[11px] text-yellow-600">
                    Showing partial content ({fileContent.total_lines} total
                    lines)
                  </p>
                )}
              </div>
              <div className="flex-1">
                <CodeViewer
                  content={fileContent.content}
                  filePath={selectedFilePath}
                />
              </div>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
              Select a file to view
            </div>
          )}
        </div>

        {/* Right: Changesets */}
        {selectedWorkspaceId && (
          <div className="w-[300px] shrink-0 overflow-auto border-l p-3 scrollbar-thin">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
              Changesets
            </p>
            <ChangesetPanel
              changesets={changesets}
              onViewDiff={setViewingDiffId}
            />
          </div>
        )}
      </div>
    </div>
  );
}
