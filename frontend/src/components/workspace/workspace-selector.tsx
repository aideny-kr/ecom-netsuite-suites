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
      <SelectTrigger className="w-[240px]">
        <SelectValue placeholder="Select workspace..." />
      </SelectTrigger>
      <SelectContent>
        {workspaces.map((ws) => (
          <SelectItem key={ws.id} value={ws.id}>
            {ws.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
