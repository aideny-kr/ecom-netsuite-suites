"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Workspace } from "@/lib/types";

interface WorkspaceSelectorProps {
  workspaces: Workspace[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function WorkspaceSelector({
  workspaces,
  selectedId,
  onSelect,
}: WorkspaceSelectorProps) {
  return (
    <Select value={selectedId || ""} onValueChange={onSelect}>
      <SelectTrigger className="w-[240px]" data-testid="workspace-selector">
        <SelectValue placeholder="Select workspace..." />
      </SelectTrigger>
      <SelectContent>
        {workspaces.map((ws) => (
          <SelectItem key={ws.id} value={ws.id} data-testid="workspace-option">
            {ws.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
