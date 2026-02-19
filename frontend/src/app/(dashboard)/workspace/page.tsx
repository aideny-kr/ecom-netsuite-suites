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
  Download,
  Upload,
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
import {
  useNetSuiteApiLogs,
  type NetSuiteApiLogEntry,
} from "@/hooks/use-netsuite-api-logs";
import { usePullFile, usePushFile } from "@/hooks/use-netsuite-file-ops";
import { Panel, Group as PanelGroup, Separator as PanelResizeHandle } from "react-resizable-panels";
import { Database, FileCode, X } from "lucide-react";
import { useMockData } from "@/hooks/use-mock-data";

type BottomTab = "chat" | "changesets" | "runs" | "logs" | "testdata";

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

function ApiLogsPanel({ logs }: { logs: NetSuiteApiLogEntry[] }) {
  if (logs.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-[12px] text-muted-foreground">
        No API logs yet. Trigger a sync or discovery to generate logs.
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <p className="text-[11px] font-medium text-muted-foreground mb-2">
        {logs.length} recent API calls
      </p>
      {logs.map((log) => (
        <div
          key={log.id}
          className="rounded border px-2 py-1.5 text-[11px] space-y-0.5"
        >
          <div className="flex items-center gap-2">
            <span className="font-mono font-bold">{log.method}</span>
            <span className="flex-1 truncate text-muted-foreground font-mono">
              {log.url.replace(/https?:\/\/[^/]+/, "")}
            </span>
            <span
              className={cn(
                "font-mono font-bold",
                log.response_status && log.response_status < 300
                  ? "text-green-600"
                  : log.response_status && log.response_status >= 400
                    ? "text-red-600"
                    : "text-muted-foreground",
              )}
            >
              {log.response_status || "ERR"}
            </span>
          </div>
          <div className="flex items-center gap-2 text-muted-foreground">
            <span>{log.source}</span>
            {log.response_time_ms != null && (
              <span>{log.response_time_ms}ms</span>
            )}
            {log.created_at && <span>{timeAgo(log.created_at)}</span>}
          </div>
          {log.error_message && (
            <div className="text-destructive truncate">
              {log.error_message}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function TestDataPanel() {
  const [query, setQuery] = useState("SELECT id, companyname, email FROM customer WHERE ROWNUM <= 10");
  const [maskPii, setMaskPii] = useState(true);
  const mockData = useMockData();

  async function handleExecute() {
    if (!query.trim()) return;
    await mockData.mutateAsync({ query: query.trim(), limit: 100, mask_pii: maskPii });
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      <div className="flex items-center gap-2">
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          rows={2}
          className="flex-1 rounded border bg-background px-2 py-1.5 text-[12px] font-mono resize-none focus:outline-none focus:ring-1 focus:ring-primary"
          placeholder="SELECT id, email FROM customer WHERE ROWNUM <= 10"
        />
        <div className="flex flex-col gap-1.5">
          <label className="flex items-center gap-1 text-[11px] text-muted-foreground cursor-pointer">
            <input
              type="checkbox"
              checked={maskPii}
              onChange={(e) => setMaskPii(e.target.checked)}
              className="h-3 w-3"
            />
            Mask PII
          </label>
          <Button
            size="sm"
            className="h-7 text-[11px]"
            onClick={handleExecute}
            disabled={mockData.isPending || !query.trim()}
          >
            {mockData.isPending ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <Database className="mr-1 h-3 w-3" />
            )}
            Run
          </Button>
        </div>
      </div>

      {mockData.error && (
        <p className="text-[11px] text-destructive">
          {mockData.error instanceof Error ? mockData.error.message : "Query failed"}
        </p>
      )}

      {mockData.data && (
        <div className="flex-1 overflow-auto">
          <p className="text-[11px] text-muted-foreground mb-1">
            {mockData.data.row_count} rows{mockData.data.masked ? " (PII masked)" : ""}
          </p>
          {mockData.data.columns.length > 0 && (
            <table className="w-full text-[11px] border-collapse">
              <thead>
                <tr>
                  {mockData.data.columns.map((col) => (
                    <th key={col} className="border px-2 py-1 text-left font-medium bg-muted/50">
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {mockData.data.data.map((row, i) => (
                  <tr key={i}>
                    {mockData.data!.columns.map((col) => (
                      <td key={col} className="border px-2 py-1 font-mono">
                        {String(row[col] ?? "")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
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
  const [bottomTab, setBottomTab] = useState<BottomTab>("chat");
  const [showPushConfirm, setShowPushConfirm] = useState(false);
  const [openTabs, setOpenTabs] = useState<Array<{ id: string; path: string }>>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);

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
  const { data: apiLogs = [] } = useNetSuiteApiLogs({ limit: 50 });
  const pullFile = usePullFile();
  const pushFile = usePushFile();

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
  const selectedWorkspace = workspaces.find((ws) => ws.id === selectedWorkspaceId);
  const isNetSuiteWorkspace = selectedWorkspace?.name === "NetSuite Scripts";

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

  const handleWorkspaceSwitch = useCallback((wsId: string | null) => {
    setSelectedWorkspaceId(wsId);
    // Reset editor state for the new workspace
    setOpenTabs([]);
    setActiveTabId(null);
    setSelectedFileId(null);
    setSelectedFilePath("");
    setViewingDiffId(null);
    setSearchQuery("");
  }, []);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    try {
      const ws = await createWs.mutateAsync({
        name: newName.trim(),
        description: newDesc.trim() || undefined,
      });
      handleWorkspaceSwitch(ws.id);
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
    setActiveTabId(fileId);
    setViewingDiffId(null);
    setOpenTabs((prev) => {
      if (prev.some((t) => t.id === fileId)) return prev;
      return [...prev, { id: fileId, path }];
    });
  };

  const closeTab = useCallback(
    (tabId: string) => {
      setOpenTabs((prev) => {
        const remaining = prev.filter((t) => t.id !== tabId);
        if (activeTabId === tabId) {
          const last = remaining[remaining.length - 1];
          if (last) {
            setActiveTabId(last.id);
            setSelectedFileId(last.id);
            setSelectedFilePath(last.path);
          } else {
            setActiveTabId(null);
            setSelectedFileId(null);
            setSelectedFilePath("");
          }
        }
        return remaining;
      });
    },
    [activeTabId],
  );

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
    setBottomTab("changesets");
  }, []);

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-3 border-b px-4 py-2.5">
        <WorkspaceSelector
          workspaces={workspaces}
          selectedId={selectedWorkspaceId}
          onSelect={handleWorkspaceSwitch}
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

      {/* Main content — resizable panels */}
      <PanelGroup orientation="horizontal" className="flex-1 overflow-hidden">
        {/* Left: File Tree */}
        <Panel defaultSize={18} minSize={10} maxSize={35}>
          <div className="h-full overflow-auto bg-muted/20 p-2 scrollbar-thin">
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
        </Panel>

        <PanelResizeHandle className="w-px bg-border hover:bg-primary/40 transition-colors cursor-col-resize" />

        {/* Right: Editor + Bottom Panel */}
        <Panel defaultSize={82}>
          <PanelGroup orientation="vertical">
            {/* Top: Editor with tab bar */}
            <Panel defaultSize={65} minSize={30}>
              <div className="flex h-full flex-col overflow-hidden">
                {/* Tab bar */}
                {openTabs.length > 0 && (
                  <div className="flex items-center border-b overflow-x-auto scrollbar-thin shrink-0 bg-muted/30">
                    {openTabs.map((tab) => (
                      <div
                        key={tab.id}
                        onClick={() => {
                          setActiveTabId(tab.id);
                          setSelectedFileId(tab.id);
                          setSelectedFilePath(tab.path);
                          setViewingDiffId(null);
                        }}
                        className={cn(
                          "flex items-center gap-1.5 px-3 py-1.5 text-[12px] border-r cursor-pointer group shrink-0",
                          activeTabId === tab.id
                            ? "bg-background text-foreground border-b-2 border-b-primary"
                            : "text-muted-foreground hover:text-foreground hover:bg-background/50",
                        )}
                      >
                        <FileCode className="h-3 w-3" />
                        <span className="truncate max-w-[120px]">
                          {tab.path.split("/").pop()}
                        </span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            closeTab(tab.id);
                          }}
                          className="ml-0.5 opacity-0 group-hover:opacity-100 hover:text-destructive"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}

                {/* Editor body */}
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
                      <div className="flex items-center justify-between border-b px-4 py-2">
                        <p className="text-[13px] font-medium font-mono">
                          {selectedFilePath}
                        </p>
                        {nsConnection && isNetSuiteWorkspace && selectedFileId && selectedWorkspaceId && (
                          <div className="flex items-center gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 text-[11px]"
                              disabled={pullFile.isPending}
                              onClick={() =>
                                pullFile.mutate({
                                  fileId: selectedFileId,
                                  workspaceId: selectedWorkspaceId,
                                })
                              }
                            >
                              {pullFile.isPending ? (
                                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                              ) : (
                                <Download className="mr-1 h-3 w-3" />
                              )}
                              Pull
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 text-[11px]"
                              disabled={pushFile.isPending}
                              onClick={() => setShowPushConfirm(true)}
                            >
                              {pushFile.isPending ? (
                                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                              ) : (
                                <Upload className="mr-1 h-3 w-3" />
                              )}
                              Push
                            </Button>
                            <AlertDialog open={showPushConfirm} onOpenChange={setShowPushConfirm}>
                              <AlertDialogContent>
                                <AlertDialogHeader>
                                  <AlertDialogTitle>Push to NetSuite {nsEnv.label}?</AlertDialogTitle>
                                  <AlertDialogDescription>
                                    This will overwrite the file on your {nsEnv.label} account. Make sure you have reviewed your changes.
                                  </AlertDialogDescription>
                                </AlertDialogHeader>
                                <AlertDialogFooter>
                                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                                  <AlertDialogAction
                                    onClick={() => {
                                      pushFile.mutate({
                                        fileId: selectedFileId,
                                        workspaceId: selectedWorkspaceId,
                                      });
                                    }}
                                  >
                                    Push Changes
                                  </AlertDialogAction>
                                </AlertDialogFooter>
                              </AlertDialogContent>
                            </AlertDialog>
                          </div>
                        )}
                      </div>
                      {fileContent.truncated && (
                        <div className="border-b px-4 py-1">
                          <p className="text-[11px] text-yellow-600">
                            Showing partial content ({fileContent.total_lines} total
                            lines)
                          </p>
                        </div>
                      )}
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
              </div>
            </Panel>

            <PanelResizeHandle className="h-px bg-border hover:bg-primary/40 transition-colors cursor-row-resize" />

            {/* Bottom: Chat + Changesets + Runs + Logs */}
            <Panel defaultSize={35} minSize={15}>
              {selectedWorkspaceId ? (
                <div className="flex h-full flex-col overflow-hidden">
                  {/* Tab bar */}
                  <div className="flex border-b shrink-0">
                    {(["chat", "changesets", "runs", "logs", "testdata"] as const).map((tab) => (
                      <button
                        key={tab}
                        onClick={() => setBottomTab(tab)}
                        className={cn(
                          "flex-1 px-2 py-2 text-[11px] font-semibold uppercase tracking-widest transition-colors",
                          bottomTab === tab
                            ? "border-b-2 border-primary text-foreground"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {tab === "testdata" ? "test data" : tab}
                      </button>
                    ))}
                  </div>

                  {/* Tab content */}
                  {bottomTab === "chat" && (
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
                  {bottomTab === "changesets" && (
                    <div className="flex-1 overflow-auto p-3 scrollbar-thin" data-testid="changeset-panel">
                      <ChangesetPanel
                        changesets={changesets}
                        onViewDiff={setViewingDiffId}
                      />
                    </div>
                  )}
                  {bottomTab === "runs" && (
                    <div className="flex-1 overflow-auto p-3 scrollbar-thin">
                      <RunsPanel runs={runs} />
                    </div>
                  )}
                  {bottomTab === "logs" && (
                    <div className="flex-1 overflow-auto p-3 scrollbar-thin">
                      <ApiLogsPanel logs={apiLogs} />
                    </div>
                  )}
                  {bottomTab === "testdata" && (
                    <div className="flex-1 overflow-hidden">
                      <TestDataPanel />
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
                  Select a workspace
                </div>
              )}
            </Panel>
          </PanelGroup>
        </Panel>
      </PanelGroup>
    </div>
  );
}
