"use client";

import { useState, useEffect, useCallback } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  Plus,
  Search,
  Brain,
  Wifi,
  WifiOff,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
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
import { RunsPanel } from "@/components/workspace/runs-panel";
import { ImportDialog } from "@/components/workspace/import-dialog";
import { WorkspaceChatPanel } from "@/components/workspace/workspace-chat-panel";
import {
  useWorkspaces,
  useCreateWorkspace,
  useWorkspaceFiles,
  useFileContent,
  useSearchFiles,
} from "@/hooks/use-workspace";
import { useChangesets, useChangesetDiff } from "@/hooks/use-changesets";
import { useRuns } from "@/hooks/use-runs";
import { useAiSettings } from "@/hooks/use-ai-settings";
import { useConnections } from "@/hooks/use-connections";
import { useMcpConnectors } from "@/hooks/use-mcp-connectors";
import {
  useSuiteScriptSyncStatus,
  useTriggerSuiteScriptSync,
} from "@/hooks/use-suitescript-sync";
import { AI_PROVIDERS, AI_MODELS } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useQueryClient } from "@tanstack/react-query";

type RightTab = "changesets" | "runs" | "chat";

function detectNetSuiteEnvironment(accountId: string | undefined): {
  label: string;
  variant: "sandbox" | "production";
} {
  if (!accountId) return { label: "Unknown", variant: "production" };
  const upper = accountId.toUpperCase();
  if (
    upper.includes("TSTDRV") ||
    upper.includes("-SB") ||
    upper.includes("_SB")
  ) {
    return { label: "Sandbox", variant: "sandbox" };
  }
  return { label: "Production", variant: "production" };
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor(
    (Date.now() - new Date(dateStr).getTime()) / 1000,
  );
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

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
  const [rightTab, setRightTab] = useState<RightTab>("changesets");

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
  const { data: runs = [] } = useRuns(selectedWorkspaceId);
  const { data: diffData } = useChangesetDiff(viewingDiffId);
  const searchParams = useSearchParams();
  const router = useRouter();
  const queryClient = useQueryClient();

  // Context bar hooks
  const { data: aiConfig } = useAiSettings();
  const { data: connections = [] } = useConnections();
  const { data: mcpConnectors = [] } = useMcpConnectors();
  const { data: syncStatus } = useSuiteScriptSyncStatus();
  const triggerSync = useTriggerSuiteScriptSync();

  // AI model display
  const aiProvider = aiConfig?.ai_provider || "";
  const aiModel = aiConfig?.ai_model || "";
  const providerLabel =
    AI_PROVIDERS.find((p) => p.value === aiProvider)?.label || "Platform Default";
  const modelLabel =
    (aiProvider && AI_MODELS[aiProvider]?.find((m) => m.value === aiModel)?.label) ||
    providerLabel;

  // NetSuite connection
  const nsConnection = connections.find(
    (c) => c.provider === "netsuite" && c.status === "active",
  );
  const nsAccountId = nsConnection?.metadata_json?.account_id as
    | string
    | undefined;
  const nsEnv = detectNetSuiteEnvironment(nsAccountId);
  const hasMcp = mcpConnectors.some(
    (mc) => mc.provider === "netsuite_mcp" && mc.is_enabled,
  );

  // SuiteScript sync
  const isSyncing =
    syncStatus?.status === "in_progress" || syncStatus?.status === "pending";
  const handleSync = useCallback(() => {
    triggerSync.mutate(undefined, {
      onSuccess: () => {
        queryClient.invalidateQueries({
          queryKey: ["workspace-files", selectedWorkspaceId],
        });
      },
    });
  }, [triggerSync, queryClient, selectedWorkspaceId]);

  // Deep-link: auto-select workspace + file from ?file= query param (e.g. from chat mention)
  const findFileInTree = useCallback(
    (nodes: typeof fileTree, path: string): { id: string; path: string } | null => {
      for (const node of nodes) {
        if (!node.is_directory && node.path === path) {
          return { id: node.id, path: node.path };
        }
        if (node.children) {
          const found = findFileInTree(node.children, path);
          if (found) return found;
        }
      }
      return null;
    },
    [],
  );

  useEffect(() => {
    const fileParam = searchParams.get("file");
    const workspaceParam = searchParams.get("workspace");
    if (!fileParam) return;

    if (
      workspaceParam &&
      selectedWorkspaceId !== workspaceParam &&
      workspaces.some((ws) => ws.id === workspaceParam)
    ) {
      setSelectedWorkspaceId(workspaceParam);
      return;
    }

    // Auto-select first workspace if none selected
    if (!selectedWorkspaceId && workspaces.length > 0) {
      setSelectedWorkspaceId(workspaces[0].id);
      return; // fileTree will load on next render
    }

    // Find and select the file in the tree
    if (fileTree.length > 0) {
      const match = findFileInTree(fileTree, fileParam);
      if (match) {
        setSelectedFileId(match.id);
        setSelectedFilePath(match.path);
        setViewingDiffId(null);
      }
    }
  }, [searchParams, fileTree, workspaces, selectedWorkspaceId, findFileInTree]);

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

  const handleMentionClick = useCallback(
    (filePath: string) => {
      if (!fileTree.length) return;
      const match = findFileInTree(fileTree, filePath);
      if (match) {
        handleFileSelect(match.id, match.path);
      }
    },
    [fileTree, findFileInTree],
  );

  const handleChatViewDiff = useCallback((changesetId: string) => {
    setViewingDiffId(changesetId);
  }, []);

  const handleChangesetAction = useCallback(() => {
    setRightTab("changesets");
  }, []);

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
                <Label htmlFor="workspace-name" className="text-[13px]">Name</Label>
                <Input
                  id="workspace-name"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="My SDF Project"
                  className="mt-1"
                />
              </div>
              <div>
                <Label htmlFor="workspace-description" className="text-[13px]">Description</Label>
                <Input
                  id="workspace-description"
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

      {/* Context Bar */}
      <div className="flex items-center gap-4 border-b bg-muted/30 px-4 py-1.5 text-[12px]">
        {/* AI Model */}
        <button
          className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
          onClick={() => router.push("/settings")}
        >
          <Brain className="h-3.5 w-3.5" />
          <span>{modelLabel}</span>
        </button>

        <div className="h-4 w-px bg-border" />

        {/* NetSuite Connection */}
        <div className="flex items-center gap-1.5">
          {nsConnection ? (
            <>
              <Wifi className="h-3.5 w-3.5 text-green-500" />
              <span className="text-muted-foreground">
                {nsAccountId || "Connected"}
              </span>
              <Badge
                variant="outline"
                className={cn(
                  "px-1.5 py-0 text-[10px]",
                  nsEnv.variant === "sandbox"
                    ? "border-amber-500/50 text-amber-600"
                    : "border-green-500/50 text-green-600",
                )}
              >
                {nsEnv.label}
              </Badge>
              <span className="text-[11px] text-muted-foreground">
                {hasMcp ? "MCP \u2713" : ""}{" "}
                {nsConnection ? "OAuth \u2713" : ""}
              </span>
            </>
          ) : (
            <button
              className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
              onClick={() => router.push("/settings")}
            >
              <WifiOff className="h-3.5 w-3.5" />
              <span>Not Connected</span>
            </button>
          )}
        </div>

        <div className="h-4 w-px bg-border" />

        {/* SuiteScript Sync */}
        <button
          onClick={handleSync}
          disabled={!nsConnection || isSyncing}
          className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {isSyncing ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>Syncing...</span>
            </>
          ) : syncStatus?.status === "failed" ? (
            <>
              <AlertCircle className="h-3.5 w-3.5 text-destructive" />
              <span className="text-destructive">Sync Failed</span>
            </>
          ) : syncStatus?.status === "completed" ? (
            <>
              <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
              <span>
                {syncStatus.total_files_loaded} files
              </span>
            </>
          ) : (
            <>
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Sync Scripts</span>
            </>
          )}
        </button>

        {syncStatus?.last_sync_at && (
          <span className="ml-auto text-[11px] text-muted-foreground">
            Last sync: {timeAgo(syncStatus.last_sync_at)}
          </span>
        )}
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
                <p className="text-[11px] text-muted-foreground">
                  {diffData.files.length} file(s) changed
                </p>
              </div>
              <div className="flex-1 overflow-auto">
                {diffData.files.map((file, idx) => (
                  <div key={idx} className="border-b last:border-b-0">
                    <div className="px-4 py-1.5 text-[12px] font-mono bg-muted/30 border-b">
                      {file.file_path}{" "}
                      <span className="text-muted-foreground">
                        ({file.operation})
                      </span>
                    </div>
                    <div style={{ height: "400px" }}>
                      <DiffViewer
                        original={file.original_content}
                        modified={file.modified_content}
                        filePath={file.file_path}
                      />
                    </div>
                  </div>
                ))}
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

        {/* Right: Tabbed Panel */}
        {selectedWorkspaceId && (
          <div className="w-[340px] shrink-0 flex flex-col overflow-hidden border-l">
            {/* Tab bar */}
            <div className="flex border-b">
              {(["changesets", "runs", "chat"] as const).map((tab) => (
                <button
                  key={tab}
                  onClick={() => setRightTab(tab)}
                  className={cn(
                    "flex-1 px-2 py-2 text-[11px] font-semibold uppercase tracking-widest transition-colors",
                    rightTab === tab
                      ? "border-b-2 border-primary text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {tab}
                </button>
              ))}
            </div>

            {/* Tab content */}
            {rightTab === "changesets" && (
              <div className="flex-1 overflow-auto p-3 scrollbar-thin" data-testid="changeset-panel">
                <ChangesetPanel
                  changesets={changesets}
                  onViewDiff={setViewingDiffId}
                />
              </div>
            )}
            {rightTab === "runs" && (
              <div className="flex-1 overflow-auto p-3 scrollbar-thin">
                <RunsPanel runs={runs} />
              </div>
            )}
            {rightTab === "chat" && (
              <div className="flex-1 overflow-hidden">
                <WorkspaceChatPanel
                  workspaceId={selectedWorkspaceId}
                  currentFilePath={selectedFilePath || undefined}
                  onMentionClick={handleMentionClick}
                  onViewDiff={handleChatViewDiff}
                  onChangesetAction={handleChangesetAction}
                />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
