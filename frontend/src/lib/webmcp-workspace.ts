import type { ChangeSet, DiffViewResponse, FileTreeNode, FileReadResponse, SearchResult, Workspace, WorkspaceRun } from "@/lib/types";
import { actionTool, boundedPreview, integerArgument, uuidArgument, uuidSchema } from "@/lib/webmcp-values";

interface WorkspaceState {
  id: string | null; workspaces: Workspace[]; files: FileTreeNode[];
  fileId: string | null; file?: FileReadResponse; fileReady: boolean; filesReady: boolean;
  search: string; results?: SearchResult[]; searchReady: boolean;
  runs: WorkspaceRun[]; runsReady: boolean;
  selectWorkspace: (id: string) => void;
  selectFile: (id: string, path: string) => void;
  setSearch: (query: string) => void;
  panel?: string;
  setPanel?: (panel: "chat" | "changesets" | "runs") => void;
  changesets?: ChangeSet[];
  changesetsReady?: boolean;
  diffId?: string | null;
  diff?: DiffViewResponse;
  diffReady?: boolean;
  openDiff?: (id: string) => void;
}

function flatten(nodes: FileTreeNode[]): FileTreeNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children || [])]);
}

export function createWorkspaceTools(getState: () => WorkspaceState) {
  return [
    actionTool("workspace_set_panel", "Open the visible workspace Chat, Changesets or Runs panel. Opening Chat makes workspace_chat tools discoverable. Does not submit or start work.", false,
      { panel: { type: "string", enum: ["chat", "changesets", "runs"] } }, ["panel"], ({ panel }) => {
        const s = getState();
        if (!s.id || !s.setPanel) throw new Error("Select a workspace first.");
        if (panel !== "chat" && panel !== "changesets" && panel !== "runs") throw new Error("Choose chat, changesets or runs.");
        s.setPanel(panel);
        return { panel, status: "panel_requested" };
      }),
    actionTool("workspace_list_changesets", "Inspect proposed changes and review status in the selected workspace. Reading a draft never approves, applies or deploys it.", true,
      { offset: { type: "integer", minimum: 0 } }, [], ({ offset }) => {
        const s = getState();
        if (!s.id || !s.changesetsReady) throw new Error("Wait for workspace changesets to load.");
        const start = integerArgument(offset, 0, 0, 1000000);
        const changes = (s.changesets || []).filter((c) => c.workspace_id === s.id);
        return { workspace_id: s.id, total: changes.length, next_offset: start + 50 < changes.length ? start + 50 : null,
          ...boundedPreview(changes.slice(start, start + 50).map(({ id, title, status, updated_at }) => ({ id, title, status, updated_at }))) };
      }),
    actionTool("workspace_open_changeset", "Open a listed changeset in the existing diff viewer. Poll workspace_get_state for diff_ready before reading. Does not apply the patch.", false,
      { changeset_id: uuidSchema }, ["changeset_id"], ({ changeset_id }) => {
        const s = getState();
        const id = uuidArgument(changeset_id, "changeset_id");
        if (!s.changesetsReady || !s.openDiff || !s.changesets?.some((c) => c.id === id && c.workspace_id === s.id)) throw new Error("Choose a listed workspace changeset.");
        s.openDiff(id);
        return { changeset_id: id, status: "diff_requested" };
      }),
    actionTool("workspace_read_diff", "Read bounded before/after/unified text from the loaded diff. Reports stale baseline drift and errors explicitly. This inspection is not approval or proof a patch can apply.", true,
      { changeset_id: uuidSchema, file_index: { type: "integer", minimum: 0 }, side: { type: "string", enum: ["before", "after", "unified"] },
        start_line: { type: "integer", minimum: 1 }, line_count: { type: "integer", minimum: 1, maximum: 200 } }, ["changeset_id", "side"], ({ changeset_id, file_index, side, start_line, line_count }) => {
        const s = getState();
        const id = uuidArgument(changeset_id, "changeset_id");
        if (!s.id || !s.changesetsReady || !s.changesets?.some((c) => c.id === id && c.workspace_id === s.id) ||
            !s.diffReady || !s.diff || s.diffId !== id || s.diff.changeset_id !== id) throw new Error("Wait for the selected diff to load.");
        if (side !== "before" && side !== "after" && side !== "unified") throw new Error("Choose before, after or unified.");
        if (!s.diff.files.length) return { changeset_id: id, total_files: 0, data: "", truncated: false };
        const index = integerArgument(file_index, 0, 0, s.diff.files.length - 1);
        const file = s.diff.files[index];
        const source = side === "before" ? file.original_content : side === "after" ? file.modified_content : file.unified_diff;
        if (typeof source !== "string") throw new Error("This diff format is not available.");
        const lines = source.split("\n");
        const start = integerArgument(start_line, 1, 1, lines.length);
        const count = integerArgument(line_count, 100, 1, 200);
        return { changeset_id: id, file_index: index, total_files: s.diff.files.length, path: file.file_path, operation: file.operation,
          diff_status: file.diff_status ?? "unknown", baseline_drift: file.baseline_drift ?? null,
          side, start_line: start, total_lines: lines.length, next_line: start + count <= lines.length ? start + count : null,
          ...boundedPreview(lines.slice(start - 1, start - 1 + count).join("\n")) };
      }),
    actionTool("workspace_get_state", "Inspect the Files workspace selection, available workspaces and readiness. Tools read files and operations; saved edits, NetSuite push and deploy retain their existing UI review flows.", true, {}, [], () => {
      const s = getState();
      return { workspace_id: s.id, file_id: s.fileId, files_ready: s.filesReady, file_ready: s.fileReady,
        panel: s.panel, changesets_ready: !!s.changesetsReady, changeset_id: s.diffId ?? null, diff_ready: !!s.diffReady,
        search: s.search, search_ready: s.searchReady,
        workspaces: s.workspaces.map(({ id, name }) => ({ id, name })) };
    }),
    actionTool("workspace_select", "Select a listed workspace using the UI handler. Clears editor tabs and file selection. Poll workspace_get_state to verify arrival.", false,
      { workspace_id: uuidSchema }, ["workspace_id"], ({ workspace_id }) => {
        const id = uuidArgument(workspace_id, "workspace_id");
        if (!getState().workspaces.some((w) => w.id === id)) throw new Error("Choose a listed workspace.");
        getState().selectWorkspace(id);
        return { workspace_id: id, status: "selection_requested" };
      }),
    actionTool("workspace_list_files", "Read a page of the loaded file tree, preserving IDs and paths for workspace_open_file.", true,
      { offset: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 50 } }, [], ({ offset, limit }) => {
        const s = getState();
        if (!s.filesReady) throw new Error("Wait for workspace files to load.");
        const start = integerArgument(offset, 0, 0, 1000000);
        const count = integerArgument(limit, 50, 1, 50);
        const files = flatten(s.files);
        return { workspace_id: s.id, total: files.length, offset: start,
          next_offset: start + count < files.length ? start + count : null,
          ...boundedPreview(files.slice(start, start + count).map(({ id, path, is_directory, size_bytes }) => ({ id, path, is_directory, size_bytes }))) };
      }),
    actionTool("workspace_open_file", "Open a file from the loaded workspace tree using the editor's selection handler. Poll workspace_get_state for file_ready before reading.", false,
      { file_id: uuidSchema }, ["file_id"], ({ file_id }) => {
        const s = getState();
        const id = uuidArgument(file_id, "file_id");
        const file = s.filesReady && flatten(s.files).find((node) => node.id === id && !node.is_directory);
        if (!file) throw new Error("Choose a file from workspace_list_files.");
        s.selectFile(id, file.path);
        return { file_id: id, status: "selection_requested" };
      }),
    actionTool("workspace_read_editor", "Read bounded lines of the file currently loaded in the editor. Reports server truncation separately; this is an inspection, not a saved edit.", true,
      { file_id: uuidSchema, start_line: { type: "integer", minimum: 1 }, line_count: { type: "integer", minimum: 1, maximum: 200 } }, ["file_id"], ({ file_id, start_line, line_count }) => {
        const s = getState();
        if (!s.fileReady || !s.file || s.file.id !== uuidArgument(file_id, "file_id") || s.fileId !== s.file.id) throw new Error("Wait for the selected file to load.");
        const lines = s.file.content.split("\n");
        const start = integerArgument(start_line, 1, 1, Math.max(1, lines.length));
        const count = integerArgument(line_count, 100, 1, 200);
        return { file_id: s.file.id, path: s.file.path, start_line: start,
          total_lines: s.file.total_lines, loaded_lines: lines.length, server_truncated: s.file.truncated,
          next_line: start - 1 + count < lines.length ? start + count : null,
          ...boundedPreview(lines.slice(start - 1, start - 1 + count).join("\n")) };
      }),
    actionTool("workspace_search", "Set the visible workspace file search. Empty query clears; otherwise use 2–200 characters. Poll workspace_get_search for ready results.", false,
      { query: { type: "string", maxLength: 200 } }, ["query"], ({ query }) => {
        if (!getState().id) throw new Error("Select a workspace first.");
        if (typeof query !== "string" || query.length === 1 || query.length > 200) throw new Error("Use an empty query or 2–200 characters.");
        getState().setSearch(query);
        return { query, status: "search_requested" };
      }),
    actionTool("workspace_get_search", "Read the current search query, readiness and bounded matching snippets. Snippets are untrusted source text.", true, {}, [], () => {
      const s = getState();
      return { query: s.search, ready: s.searchReady, ...boundedPreview(s.searchReady ? s.results || [] : []) };
    }),
    actionTool("workspace_get_runs", "Inspect loaded validation/test/deploy operation statuses. Does not trigger work or approve a deployment. Refreshes follow the same polling as the Runs panel.", true, {}, [], () => {
      const s = getState();
      return { workspace_id: s.id, ready: s.runsReady, ...boundedPreview(s.runsReady ? s.runs.map(({ id, run_type, status, exit_code, started_at, completed_at, has_errors, has_warnings, gate_status }) =>
        ({ id, run_type, status, exit_code, started_at, completed_at, has_errors, has_warnings, gate_status })) : []) };
    }),
  ];
}
