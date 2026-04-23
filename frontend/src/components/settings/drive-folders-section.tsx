"use client";

import { useState } from "react";
import { Folder, RefreshCw, Trash2 } from "lucide-react";
import {
  useAddDriveFolder,
  useDriveFolders,
  useRemoveDriveFolder,
  useSyncDriveFolder,
  useToggleDriveFolder,
} from "@/hooks/useDriveFolders";

function formatSync(status: string, at: string | null): string {
  if (status === "syncing") return "Syncing…";
  if (status === "error") return "Sync failed";
  if (!at) return "Never synced";
  const ts = new Date(at).getTime();
  if (Number.isNaN(ts)) return "Never synced";
  const deltaMs = Date.now() - ts;
  const hours = Math.floor(deltaMs / 3_600_000);
  if (hours < 1) return "Synced just now";
  if (hours < 24) return `Synced ${hours}h ago`;
  return `Synced ${Math.floor(hours / 24)}d ago`;
}

export function DriveFoldersSection() {
  const { data: folders, isLoading } = useDriveFolders();
  const add = useAddDriveFolder();
  const remove = useRemoveDriveFolder();
  const toggle = useToggleDriveFolder();
  const sync = useSyncDriveFolder();
  const [input, setInput] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function onAdd(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await add.mutateAsync(input.trim());
      setInput("");
    } catch (ex: unknown) {
      setErr(ex instanceof Error ? ex.message : "Failed to add folder");
    }
  }

  return (
    <div className="animate-fade-in rounded-xl border bg-card p-5 shadow-soft space-y-4">
      <div>
        <h3 className="text-[15px] font-semibold text-foreground">Google Drive Knowledge</h3>
        <p className="text-[13px] text-muted-foreground">
          Register Drive folders whose contents are embedded and cited in chat. Uses the existing
          Google Sheets service account.
        </p>
      </div>

      <form onSubmit={onAdd} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Paste a Drive folder URL or ID"
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-[13px]"
        />
        <button
          type="submit"
          disabled={!input.trim() || add.isPending}
          className="rounded-md bg-primary px-4 py-2 text-[13px] font-medium text-primary-foreground disabled:opacity-50"
        >
          {add.isPending ? "Adding…" : "Add"}
        </button>
      </form>
      {err && <p className="text-[13px] text-red-600">{err}</p>}

      {isLoading && <p className="text-[13px] text-muted-foreground">Loading…</p>}
      {folders && folders.length === 0 && (
        <p className="text-[13px] text-muted-foreground">No folders registered.</p>
      )}

      <ul className="space-y-2">
        {folders?.map((f) => (
          <li key={f.id} className="flex items-center justify-between rounded-md border p-3">
            <div className="flex items-center gap-3">
              <Folder className="size-4 text-muted-foreground" />
              <div>
                <p className="text-[13px] font-medium">{f.folder_name}</p>
                <p className="text-[11px] text-muted-foreground">
                  {formatSync(f.sync_status, f.last_synced_at)} · {f.chunk_count} chunks ·
                  {" "}
                  {f.file_count} files
                </p>
                {f.last_sync_error && (
                  <p className="text-[11px] text-red-600">{f.last_sync_error}</p>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1 text-[11px]">
                <input
                  type="checkbox"
                  checked={f.is_enabled}
                  onChange={(e) => toggle.mutateAsync({ id: f.id, is_enabled: e.target.checked })}
                />
                Enabled
              </label>
              <button
                onClick={() => sync.mutateAsync(f.id)}
                className="flex items-center gap-1 rounded-md border px-2 py-1 text-[11px]"
                disabled={f.sync_status === "syncing"}
              >
                <RefreshCw className="size-3" /> Re-Sync
              </button>
              <button
                onClick={() => {
                  if (
                    typeof window !== "undefined" &&
                    window.confirm(`Remove folder "${f.folder_name}"? Indexed chunks will be deleted.`)
                  ) {
                    remove.mutateAsync(f.id);
                  }
                }}
                className="flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] text-red-600"
              >
                <Trash2 className="size-3" /> Remove
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
