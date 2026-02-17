"use client";

import { useState } from "react";
import { ChevronRight, File, Folder, FolderOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FileTreeNode } from "@/lib/types";

interface FileTreeProps {
  nodes: FileTreeNode[];
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}

export function FileTree({ nodes, onFileSelect, selectedFileId }: FileTreeProps) {
  return (
    <div className="text-[13px]" data-testid="file-tree">
      {nodes.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          depth={0}
          onFileSelect={onFileSelect}
          selectedFileId={selectedFileId}
        />
      ))}
    </div>
  );
}

function TreeNode({
  node,
  depth,
  onFileSelect,
  selectedFileId,
}: {
  node: FileTreeNode;
  depth: number;
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}) {
  const [expanded, setExpanded] = useState(depth < 1);
  const isSelected = node.id === selectedFileId;

  if (node.is_directory) {
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className={cn(
            "flex w-full items-center gap-1.5 rounded-md px-2 py-1 hover:bg-accent",
          )}
          style={{ paddingLeft: `${depth * 16 + 8}px` }}
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
              expanded && "rotate-90",
            )}
          />
          {expanded ? (
            <FolderOpen className="h-3.5 w-3.5 shrink-0 text-yellow-500" />
          ) : (
            <Folder className="h-3.5 w-3.5 shrink-0 text-yellow-500" />
          )}
          <span className="truncate">{node.name}</span>
        </button>
        {expanded && node.children && (
          <div>
            {node.children.map((child) => (
              <TreeNode
                key={child.id}
                node={child}
                depth={depth + 1}
                onFileSelect={onFileSelect}
                selectedFileId={selectedFileId}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <button
      onClick={() => onFileSelect(node.id, node.path)}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-md px-2 py-1 hover:bg-accent",
        isSelected && "bg-accent font-medium",
      )}
      style={{ paddingLeft: `${depth * 16 + 24}px` }}
    >
      <File className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate">{node.name}</span>
    </button>
  );
}
