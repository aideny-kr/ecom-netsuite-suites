"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
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
  FileCode,
  FolderOpen,
  PanelLeftClose,
  PanelLeftOpen,
  Database,
  X,
  TreePine,
  LayoutGrid,
  Terminal,
  ChevronRight,
  Keyboard,
  Sparkles,
  FlaskConical,
  Play,
  GitCompare,
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from "@/components/ui/tooltip";
import { ScrollArea } from "@/components/ui/scroll-area";
import { FileTree } from "@/components/workspace/file-tree";
import { ConstellationView } from "@/components/workspace/constellation-view";
import { ScriptContextBar } from "@/components/workspace/script-context-bar";
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
import {
  Panel,
  Group as PanelGroup,
  Separator as PanelResizeHandle,
  type PanelImperativeHandle,
} from "react-resizable-panels";
import { useMockData } from "@/hooks/use-mock-data";
import { parseSuiteScriptMetadata } from "@/lib/suitescript-parser";

// ─── Types ──────────────────────────────────────────────────────────────────

type BottomTab = "chat" | "changesets" | "runs" | "logs" | "testdata";
type FileTreeMode = "tree" | "constellation";

// ─── Utilities ──────────────────────────────────────────────────────────────

function detectNetSuiteEnvironment(accountId: string | undefined) {
  if (!accountId) return { label: "Unknown", variant: "production" as const };
  const upper = accountId.toUpperCase();
  if (upper.includes("TSTDRV") || upper.includes("-SB") || upper.includes("_SB")) {
    return { label: "Sandbox", variant: "sandbox" as const };
  }
  return { label: "Production", variant: "production" as const };
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// ─── Sub-components ─────────────────────────────────────────────────────────

function ApiLogsPanel({ logs }: { logs: NetSuiteApiLogEntry[] }) {
  if (logs.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-[12px] text-muted-foreground">
        <div className="text-center space-y-1">
          <Terminal className="h-5 w-5 mx-auto text-muted-foreground/40" />
          <p>No API logs yet</p>
          <p className="text-[10px]">Trigger a sync or file operation to generate logs</p>
        </div>
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
          className="rounded border px-2 py-1.5 text-[11px] space-y-0.5 hover:bg-accent/30 transition-colors"
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
            {log.response_time_ms != null && <span>{log.response_time_ms}ms</span>}
            {log.created_at && <span>{timeAgo(log.created_at)}</span>}
          </div>
          {log.error_message && (
            <div className="text-destructive truncate">{log.error_message}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function TestDataPanel() {
  const [query, setQuery] = useState(
    "SELECT id, companyname, email FROM customer WHERE ROWNUM <= 10",
  );
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
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              handleExecute();
            }
          }}
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
              <Play className="mr-1 h-3 w-3" />
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
                    <th key={col} className="border px-2 py-1 text-left font-medium bg-muted/50 sticky top-0">
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {mockData.data.data.map((row, i) => (
                  <tr key={i} className="hover:bg-accent/30 transition-colors">
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

      {!mockData.data && !mockData.error && !mockData.isPending && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center space-y-2 text-muted-foreground">
            <FlaskConical className="h-8 w-8 mx-auto text-muted-foreground/30" />
            <div>
              <p className="text-[12px] font-medium">Test Data Explorer</p>
              <p className="text-[11px]">
                Query your NetSuite data with SuiteQL. PII is automatically masked.
              </p>
              <p className="text-[10px] mt-1">Press <kbd className="px-1 py-0.5 rounded bg-muted border text-[9px]">⌘ Enter</kbd> to run</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Bottom Tab Icons ───────────────────────────────────────────────────────

const BOTTOM_TAB_CONFIG: Record<BottomTab, { label: string; icon: React.ReactNode }> = {
  chat: { label: "AI Chat", icon: <Sparkles className="h-3 w-3" /> },
  changesets: { label: "Changes", icon: <GitCompare className="h-3 w-3" /> },
  runs: { label: "Runs", icon: <Play className="h-3 w-3" /> },
  logs: { label: "API Logs", icon: <Terminal className="h-3 w-3" /> },
  testdata: { label: "Test Data", icon: <FlaskConical className="h-3 w-3" /> },
};

// ─── Main Component ─────────────────────────────────────────────────────────

export default function WorkspacePage() {
  // ── State ───────────────────────────────────────────────────────────
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null);
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [selectedFilePath, setSelectedFilePath] = useState<string>("");
  const [searchQuery, setSearchQuery] = useState("");
  const [viewingDiffId, setViewingDiffId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [bottomTab, setBottomTab] = useState<BottomTab>("changesets");
  const [showPushConfirm, setShowPushConfirm] = useState(false);
  const [openTabs, setOpenTabs] = useState<Array<{ id: string; path: string }>>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  const [fileTreeCollapsed, setFileTreeCollapsed] = useState(false);
  const [fileTreeMode, setFileTreeMode] = useState<FileTreeMode>("tree");
  const [searchFocused, setSearchFocused] = useState(false);
  const [isMounted, setIsMounted] = useState(false);

  const fileTreeRef = useRef<PanelImperativeHandle>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // ── Data hooks ──────────────────────────────────────────────────────
  const { data: workspaces = [] } = useWorkspaces();
  const createWs = useCreateWorkspace();
  const { data: fileTree = [] } = useWorkspaceFiles(selectedWorkspaceId);
  const { data: fileContent } = useFileContent(selectedWorkspaceId, selectedFileId);
  const { data: searchResults } = useSearchFiles(selectedWorkspaceId, searchQuery);
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

  // ── Derived state ──────────────────────────────────────────────────
  const aiProvider = aiConfig?.ai_provider || "";
  const aiModel = aiConfig?.ai_model || "";
  const providerLabel = AI_PROVIDERS.find((p) => p.value === aiProvider)?.label || "Platform Default";
  const modelLabel =
    (aiProvider && AI_MODELS[aiProvider]?.find((m) => m.value === aiModel)?.label) || providerLabel;

  const nsConnection = connections.find((c) => c.provider === "netsuite" && c.status === "active");
  const nsAccountId = nsConnection?.metadata_json?.account_id as string | undefined;
  const nsEnv = detectNetSuiteEnvironment(nsAccountId);
  const hasMcp = mcpConnectors.some((mc) => mc.provider === "netsuite_mcp" && mc.is_enabled);
  const selectedWorkspace = workspaces.find((ws) => ws.id === selectedWorkspaceId);
  const isNetSuiteWorkspace = selectedWorkspace?.name === "NetSuite Scripts";

  const isSyncing = syncStatus?.status === "in_progress" || syncStatus?.status === "pending";

  // Parse script metadata for the currently open file
  const currentFileMetadata = useMemo(() => {
    if (!selectedFilePath) return null;
    return parseSuiteScriptMetadata(fileContent?.content || null, selectedFilePath);
  }, [fileContent?.content, selectedFilePath]);

  // Count pending items for bottom tab badges
  const activeRunCount = runs.filter(
    (r) => r.status === "queued" || r.status === "running",
  ).length;
  const pendingChangesets = changesets.filter(
    (c) => c.status === "pending_review",
  ).length;

  // ── Handlers ────────────────────────────────────────────────────────
  const handleSync = useCallback(() => {
    triggerSync.mutate(undefined, {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: ["workspace-files", selectedWorkspaceId] });
      },
    });
  }, [triggerSync, queryClient, selectedWorkspaceId]);

  const findFileInTree = useCallback(
    (nodes: typeof fileTree, path: string): { id: string; path: string } | null => {
      for (const node of nodes) {
        if (!node.is_directory && node.path === path) return { id: node.id, path: node.path };
        if (node.children) {
          const found = findFileInTree(node.children, path);
          if (found) return found;
        }
      }
      return null;
    },
    [],
  );

  // Deep-link support
  useEffect(() => {
    const fileParam = searchParams.get("file");
    const workspaceParam = searchParams.get("workspace");
    if (!fileParam) return;

    if (workspaceParam && selectedWorkspaceId !== workspaceParam && workspaces.some((ws) => ws.id === workspaceParam)) {
      setSelectedWorkspaceId(workspaceParam);
      return;
    }
    if (!selectedWorkspaceId && workspaces.length > 0) {
      setSelectedWorkspaceId(workspaces[0].id);
      return;
    }
    if (fileTree.length > 0) {
      const match = findFileInTree(fileTree, fileParam);
      if (match) {
        setSelectedFileId(match.id);
        setSelectedFilePath(match.path);
        setViewingDiffId(null);
      }
    }
  }, [searchParams, fileTree, workspaces, selectedWorkspaceId, findFileInTree]);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  const handleWorkspaceSwitch = useCallback((wsId: string | null) => {
    setSelectedWorkspaceId(wsId);
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

  const handleFileSelect = useCallback(
    (fileId: string, path: string) => {
      setSelectedFileId(fileId);
      setSelectedFilePath(path);
      setActiveTabId(fileId);
      setViewingDiffId(null);
      setOpenTabs((prev) => {
        if (prev.some((t) => t.id === fileId)) return prev;
        return [...prev, { id: fileId, path }];
      });
    },
    [],
  );

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

  // ── Keyboard shortcuts ──────────────────────────────────────────────
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const isMod = e.metaKey || e.ctrlKey;

      // Cmd+K — Focus search
      if (isMod && e.key === "k") {
        e.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      }

      // Cmd+B — Toggle file tree
      if (isMod && e.key === "b") {
        e.preventDefault();
        if (fileTreeCollapsed) {
          fileTreeRef.current?.expand();
        } else {
          fileTreeRef.current?.collapse();
        }
      }

      // Escape — Clear search / deselect
      if (e.key === "Escape") {
        if (searchFocused) {
          setSearchQuery("");
          searchInputRef.current?.blur();
        }
      }

      // Cmd+W — Close active tab
      if (isMod && e.key === "w" && activeTabId) {
        e.preventDefault();
        closeTab(activeTabId);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fileTreeCollapsed, searchFocused, activeTabId, closeTab]);

  const handleMentionClick = useCallback(
    (filePath: string) => {
      if (!fileTree.length) return;
      const match = findFileInTree(fileTree, filePath);
      if (match) handleFileSelect(match.id, match.path);
    },
    [fileTree, findFileInTree, handleFileSelect],
  );

  const handleChatViewDiff = useCallback((changesetId: string) => {
    setViewingDiffId(changesetId);
  }, []);

  const handleChangesetAction = useCallback(() => {
    setBottomTab("changesets");
  }, []);

  // ── Render ──────────────────────────────────────────────────────────
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-full flex-col">
        {/* ═══ Toolbar ═══ */}
        <div className="flex items-center gap-2 border-b px-3 py-1.5 bg-background shrink-0">
          <WorkspaceSelector
            workspaces={workspaces}
            selectedId={selectedWorkspaceId}
            onSelect={handleWorkspaceSwitch}
          />

          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogTrigger asChild>
              <Button size="sm" variant="outline" className="h-7 text-[11px]">
                <Plus className="mr-1 h-3 w-3" />
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
                  <Button variant="outline" onClick={() => setCreateOpen(false)}>Cancel</Button>
                  <Button onClick={handleCreate} disabled={createWs.isPending}>
                    {createWs.isPending ? "Creating..." : "Create"}
                  </Button>
                </div>
              </div>
            </DialogContent>
          </Dialog>

          {selectedWorkspaceId && <ImportDialog workspaceId={selectedWorkspaceId} />}

          {/* Search */}
          <div className="ml-auto flex items-center gap-2">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
              <Input
                ref={searchInputRef}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onFocus={() => setSearchFocused(true)}
                onBlur={() => setSearchFocused(false)}
                placeholder="Search files..."
                className="h-7 w-[180px] pl-7 pr-8 text-[11px]"
              />
              {!searchQuery && (
                <kbd className="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none text-[9px] text-muted-foreground/50 font-mono">
                  ⌘K
                </kbd>
              )}
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          </div>
        </div>

        {/* ═══ Context Bar ═══ */}
        <div className="flex items-center gap-3 border-b bg-muted/20 px-3 py-1 text-[11px] shrink-0">
          {/* AI Model */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                className="flex items-center gap-1 text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => router.push("/settings")}
              >
                <Brain className="h-3 w-3" />
                <span>{modelLabel}</span>
              </button>
            </TooltipTrigger>
            <TooltipContent>AI model configuration</TooltipContent>
          </Tooltip>

          <div className="h-3 w-px bg-border" />

          {/* NetSuite Connection */}
          <div className="flex items-center gap-1.5">
            {nsConnection ? (
              <>
                <Wifi className="h-3 w-3 text-green-500" />
                <span className="text-muted-foreground">{nsAccountId || "Connected"}</span>
                <Badge
                  variant="outline"
                  className={cn(
                    "px-1 py-0 text-[9px] rounded-sm",
                    nsEnv.variant === "sandbox"
                      ? "border-amber-500/50 text-amber-600"
                      : "border-green-500/50 text-green-600",
                  )}
                >
                  {nsEnv.label}
                </Badge>
                {hasMcp && (
                  <span className="text-[9px] text-green-600 font-medium">MCP ✓</span>
                )}
                {nsConnection && (
                  <span className="text-[9px] text-green-600 font-medium">OAuth ✓</span>
                )}
              </>
            ) : (
              <button
                className="flex items-center gap-1 text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => router.push("/settings")}
              >
                <WifiOff className="h-3 w-3" />
                <span>Not Connected</span>
              </button>
            )}
          </div>

          <div className="h-3 w-px bg-border" />

          {/* Sync */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                onClick={handleSync}
                disabled={!nsConnection || isSyncing}
                className="flex items-center gap-1 text-muted-foreground hover:text-foreground disabled:opacity-40 transition-colors"
              >
                {isSyncing ? (
                  <>
                    <Loader2 className="h-3 w-3 animate-spin" />
                    <span>Syncing…</span>
                  </>
                ) : syncStatus?.status === "failed" ? (
                  <>
                    <AlertCircle className="h-3 w-3 text-destructive" />
                    <span className="text-destructive">Sync Failed</span>
                  </>
                ) : syncStatus?.status === "completed" ? (
                  <>
                    <CheckCircle2 className="h-3 w-3 text-green-500" />
                    <span>{syncStatus.total_files_loaded} files</span>
                  </>
                ) : (
                  <>
                    <RefreshCw className="h-3 w-3" />
                    <span>Sync</span>
                  </>
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent>
              {isSyncing ? "Syncing scripts from NetSuite…" : "Sync scripts from NetSuite"}
            </TooltipContent>
          </Tooltip>

          {syncStatus?.last_sync_at && (
            <span className="ml-auto text-[10px] text-muted-foreground/60">
              Last sync: {timeAgo(syncStatus.last_sync_at)}
            </span>
          )}

          {/* Keyboard shortcut hint */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button className="ml-auto text-muted-foreground/40 hover:text-muted-foreground transition-colors">
                <Keyboard className="h-3 w-3" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="text-[11px]">
              <div className="space-y-0.5">
                <div><kbd className="font-mono">⌘K</kbd> Search files</div>
                <div><kbd className="font-mono">⌘B</kbd> Toggle sidebar</div>
                <div><kbd className="font-mono">⌘W</kbd> Close tab</div>
              </div>
            </TooltipContent>
          </Tooltip>
        </div>

        {/* ═══ Main Layout ═══ */}
        {isMounted ? (
          <PanelGroup id="layout-final-v19" orientation="horizontal" className="flex w-full h-full overflow-hidden">
            {/* ─── Left: File Explorer ─── */}
            <Panel
              id="panel-left-v16"
              panelRef={fileTreeRef}
              defaultSize={20}
              className="!min-w-[250px]"
            >
              {fileTreeCollapsed ? (
                /* Collapsed strip */
                <div className="flex h-full flex-col items-center bg-muted/20 pt-2 gap-2 min-w-0">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => fileTreeRef.current?.expand()}
                        className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                      >
                        <PanelLeftOpen className="h-4 w-4" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="right">Expand sidebar (⌘B)</TooltipContent>
                  </Tooltip>
                </div>
              ) : (
                /* Expanded file tree */
                <div className="flex h-full flex-col bg-muted/10 min-w-0" style={{ minWidth: 220 }}>
                  {/* Header with view toggle */}
                  <div className="flex items-center justify-between border-b px-2 py-1 shrink-0">
                    <div className="flex items-center gap-1">
                      <FolderOpen className="h-3 w-3 text-muted-foreground" />
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                        Explorer
                      </span>
                    </div>
                    <div className="flex items-center gap-0.5">
                      {/* View mode toggle */}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            onClick={() => setFileTreeMode("tree")}
                            className={cn(
                              "rounded p-1 transition-colors",
                              fileTreeMode === "tree"
                                ? "bg-accent text-foreground"
                                : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
                            )}
                          >
                            <TreePine className="h-3 w-3" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>File tree view</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            onClick={() => setFileTreeMode("constellation")}
                            className={cn(
                              "rounded p-1 transition-colors",
                              fileTreeMode === "constellation"
                                ? "bg-accent text-foreground"
                                : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
                            )}
                          >
                            <LayoutGrid className="h-3 w-3" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Script type view</TooltipContent>
                      </Tooltip>

                      <div className="h-3 w-px bg-border mx-0.5" />

                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            onClick={() => fileTreeRef.current?.collapse()}
                            className="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-accent/50 transition-colors"
                          >
                            <PanelLeftClose className="h-3 w-3" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Collapse sidebar (⌘B)</TooltipContent>
                      </Tooltip>
                    </div>
                  </div>

                  {/* File tree content */}
                  <ScrollArea className="flex-1">
                    <div className="p-1.5">
                      {selectedWorkspaceId ? (
                        searchQuery && searchResults ? (
                          /* Search results */
                          <div className="space-y-0.5">
                            <p className="px-2 py-1 text-[10px] font-medium text-muted-foreground">
                              {searchResults.length} result{searchResults.length !== 1 ? "s" : ""} for &ldquo;{searchQuery}&rdquo;
                            </p>
                            {searchResults.map((r) => (
                              <button
                                key={`${r.file_id}-${r.line_number}`}
                                onClick={() => handleFileSelect(r.file_id, r.path)}
                                className={cn(
                                  "block w-full rounded-md px-2 py-1.5 text-left hover:bg-accent/60 transition-colors",
                                  selectedFileId === r.file_id && "bg-accent",
                                )}
                              >
                                <p className="truncate text-[11px] font-medium">{r.path}</p>
                                <p className="truncate text-[10px] text-muted-foreground font-mono">
                                  L{r.line_number}: {r.snippet}
                                </p>
                              </button>
                            ))}
                          </div>
                        ) : fileTreeMode === "constellation" ? (
                          <ConstellationView
                            nodes={fileTree}
                            onFileSelect={handleFileSelect}
                            selectedFileId={selectedFileId}
                          />
                        ) : (
                          <FileTree
                            nodes={fileTree}
                            onFileSelect={handleFileSelect}
                            selectedFileId={selectedFileId}
                          />
                        )
                      ) : (
                        /* No workspace selected */
                        <div className="flex h-40 items-center justify-center">
                          <div className="text-center space-y-2 text-muted-foreground">
                            <FolderOpen className="h-6 w-6 mx-auto text-muted-foreground/30" />
                            <p className="text-[11px]">Select a workspace to browse files</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </ScrollArea>
                </div>
              )}
            </Panel>

            {/* ─── Resize Handle ─── */}
            {/* ─── Horizontal Resize Handle ─── */}
            <PanelResizeHandle
              className="group relative hover:bg-primary/10 active:bg-primary/20 transition-colors"
              style={{ flexBasis: "8px" }}
            >
              <div className="pointer-events-none absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-border group-hover:bg-primary/50 transition-colors" />
            </PanelResizeHandle>

            {/* ─── Middle: Editor ─── */}
            <Panel id="panel-middle-v16" defaultSize={55} className="!min-w-[400px]">
              <div className="flex h-full w-full flex-col overflow-hidden min-w-0">
                {/* Tab bar */}
                {openTabs.length > 0 && (
                  <div className="flex items-center border-b overflow-x-auto scrollbar-thin shrink-0 bg-muted/20">
                    {openTabs.map((tab) => {
                      const tabMeta = parseSuiteScriptMetadata(null, tab.path);
                      const isActive = activeTabId === tab.id;
                      return (
                        <div
                          key={tab.id}
                          onClick={() => {
                            setActiveTabId(tab.id);
                            setSelectedFileId(tab.id);
                            setSelectedFilePath(tab.path);
                            setViewingDiffId(null);
                          }}
                          className={cn(
                            "flex items-center gap-1.5 px-3 py-1.5 text-[11px] border-r border-border/40 cursor-pointer group shrink-0 transition-colors",
                            isActive
                              ? "bg-background text-foreground border-b-2 border-b-primary"
                              : "text-muted-foreground hover:text-foreground hover:bg-background/50",
                          )}
                        >
                          {tabMeta.scriptType !== "Unknown" ? (
                            <span className={cn(
                              "inline-flex items-center justify-center rounded px-0.5 text-[8px] font-bold leading-none border",
                              tabMeta.color,
                            )}>
                              {tabMeta.scriptTypeShort}
                            </span>
                          ) : (
                            <FileCode className="h-3 w-3 shrink-0" />
                          )}
                          <span className="truncate max-w-[120px]">
                            {tab.path.split("/").pop()}
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              closeTab(tab.id);
                            }}
                            className="ml-0.5 opacity-0 group-hover:opacity-100 hover:text-destructive transition-opacity"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Script context bar */}
                {currentFileMetadata && !viewingDiffId && (
                  <ScriptContextBar metadata={currentFileMetadata} filePath={selectedFilePath} />
                )}

                {/* Editor body */}
                <div className="flex-1 overflow-hidden min-w-0">
                  {viewingDiffId && diffData ? (
                    /* Diff viewer */
                    <div className="flex h-full flex-col min-w-0">
                      <div className="border-b px-4 py-2 bg-muted/20">
                        <p className="text-[13px] font-medium">{diffData.title}</p>
                        <p className="text-[11px] text-muted-foreground">
                          {diffData.files.length} file(s) changed
                        </p>
                      </div>
                      <div className="flex-1 overflow-auto">
                        {diffData.files.map((file, idx) => (
                          <div key={idx} className="border-b last:border-b-0">
                            <div className="px-4 py-1.5 text-[12px] font-mono bg-muted/30 border-b flex items-center gap-2">
                              <ChevronRight className="h-3 w-3 text-muted-foreground" />
                              {file.file_path}
                              <Badge variant="outline" className="text-[9px] px-1 py-0">
                                {file.operation}
                              </Badge>
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
                    /* Code viewer */
                    <div className="flex h-full flex-col min-w-0">
                      <div className="flex items-center justify-between border-b px-4 py-1.5 bg-muted/10">
                        {/* Breadcrumb path */}
                        <div className="flex items-center gap-1 text-[11px] font-mono text-muted-foreground overflow-hidden">
                          {selectedFilePath.split("/").map((part, i, arr) => (
                            <span key={i} className="flex items-center gap-1 shrink-0">
                              {i > 0 && <ChevronRight className="h-2.5 w-2.5 text-muted-foreground/40" />}
                              <span className={cn(
                                i === arr.length - 1 ? "text-foreground font-medium" : "",
                              )}>
                                {part}
                              </span>
                            </span>
                          ))}
                        </div>

                        {/* File actions */}
                        {nsConnection && isNetSuiteWorkspace && selectedFileId && selectedWorkspaceId && (
                          <div className="flex items-center gap-1 shrink-0">
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-6 w-6 p-0"
                                  disabled={pullFile.isPending}
                                  onClick={() =>
                                    pullFile.mutate({
                                      fileId: selectedFileId,
                                      workspaceId: selectedWorkspaceId,
                                    })
                                  }
                                >
                                  {pullFile.isPending ? (
                                    <Loader2 className="h-3 w-3 animate-spin" />
                                  ) : (
                                    <Download className="h-3 w-3" />
                                  )}
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Pull latest from NetSuite</TooltipContent>
                            </Tooltip>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-6 w-6 p-0"
                                  disabled={pushFile.isPending}
                                  onClick={() => setShowPushConfirm(true)}
                                >
                                  {pushFile.isPending ? (
                                    <Loader2 className="h-3 w-3 animate-spin" />
                                  ) : (
                                    <Upload className="h-3 w-3" />
                                  )}
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Push to NetSuite {nsEnv.label}</TooltipContent>
                            </Tooltip>
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
                        <div className="border-b px-4 py-1 bg-yellow-500/5">
                          <p className="text-[10px] text-yellow-600">
                            Showing partial content ({fileContent.total_lines} total lines)
                          </p>
                        </div>
                      )}
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <CodeViewer content={fileContent.content} filePath={selectedFilePath} />
                      </div>
                    </div>
                  ) : (
                    /* Empty state */
                    <div className="flex h-full items-center justify-center">
                      <div className="text-center space-y-3">
                        <div className="relative mx-auto w-16 h-16">
                          <div className="absolute inset-0 rounded-full bg-primary/5" />
                          <FileCode className="absolute inset-0 m-auto h-7 w-7 text-muted-foreground/30" />
                        </div>
                        <div className="space-y-1">
                          <p className="text-[13px] font-medium text-muted-foreground">
                            {selectedWorkspaceId ? "Select a file to view" : "Open a workspace to start"}
                          </p>
                          <p className="text-[11px] text-muted-foreground/60">
                            {selectedWorkspaceId
                              ? "Click a file in the explorer or press ⌘K to search"
                              : "Create or select a workspace from the toolbar above"}
                          </p>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </Panel>

            {/* ─── Horizontal Resize Handle ─── */}
            <PanelResizeHandle
              className="group relative hover:bg-primary/10 active:bg-primary/20 transition-colors"
              style={{ flexBasis: "8px" }}
            >
              <div className="pointer-events-none absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-border group-hover:bg-primary/50 transition-colors" />
            </PanelResizeHandle>

            {/* ─── Right: Tools Panel ─── */}
            <Panel id="panel-right-v16" defaultSize={25} className="!min-w-[350px]">
              {selectedWorkspaceId ? (
                <div className="flex h-full w-full flex-col overflow-hidden bg-muted/5 border-l min-w-0" style={{ minWidth: 300 }}>
                  {/* Bottom tab bar */}
                  <div className="flex border-b shrink-0 bg-muted/10">
                    {(Object.entries(BOTTOM_TAB_CONFIG) as [BottomTab, typeof BOTTOM_TAB_CONFIG[BottomTab]][]).map(([tab, config]) => {
                      const isActive = bottomTab === tab;
                      const badge =
                        tab === "runs" && activeRunCount > 0
                          ? activeRunCount
                          : tab === "changesets" && pendingChangesets > 0
                            ? pendingChangesets
                            : null;

                      return (
                        <button
                          key={tab}
                          onClick={() => setBottomTab(tab)}
                          className={cn(
                            "flex items-center justify-center gap-1.5 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors",
                            isActive
                              ? "border-b-2 border-primary text-foreground bg-background/50"
                              : "text-muted-foreground hover:text-foreground hover:bg-accent/30",
                          )}
                        >
                          {config.icon}
                          <span>{config.label}</span>
                          {badge !== null && (
                            <span className={cn(
                              "inline-flex items-center justify-center rounded-full px-1 min-w-[14px] h-[14px] text-[8px] font-bold",
                              tab === "runs"
                                ? "bg-blue-500/20 text-blue-600"
                                : "bg-amber-500/20 text-amber-600",
                            )}>
                              {badge}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>

                  {/* Tab content */}
                  <div className="flex-1 overflow-hidden">
                    {bottomTab === "chat" && (
                      <WorkspaceChatPanel
                        workspaceId={selectedWorkspaceId}
                        currentFilePath={selectedFilePath || undefined}
                        onMentionClick={handleMentionClick}
                        onViewDiff={handleChatViewDiff}
                        onChangesetAction={handleChangesetAction}
                      />
                    )}
                    {bottomTab === "changesets" && (
                      <div className="h-full overflow-auto p-3 scrollbar-thin" data-testid="changeset-panel">
                        <ChangesetPanel changesets={changesets} onViewDiff={setViewingDiffId} />
                      </div>
                    )}
                    {bottomTab === "runs" && (
                      <div className="h-full overflow-auto p-3 scrollbar-thin">
                        <RunsPanel runs={runs} />
                      </div>
                    )}
                    {bottomTab === "logs" && (
                      <div className="h-full overflow-auto p-3 scrollbar-thin">
                        <ApiLogsPanel logs={apiLogs} />
                      </div>
                    )}
                    {bottomTab === "testdata" && <TestDataPanel />}
                  </div>
                </div>
              ) : (
                <div className="flex h-full items-center justify-center p-4">
                  <div className="text-center space-y-2">
                    <Sparkles className="h-8 w-8 mx-auto text-muted-foreground/30" />
                    <p className="text-[11px] text-muted-foreground">Select a workspace to use tools</p>
                  </div>
                </div>
              )}
            </Panel>
          </PanelGroup>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground/50" />
          </div>
        )}
      </div>
    </TooltipProvider>
  );
}
