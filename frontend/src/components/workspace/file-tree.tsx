"use client";

import { useState } from "react";
import { ChevronRight, File, Folder, FolderOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { parseSuiteScriptMetadata } from "@/lib/suitescript-parser";
import type { FileTreeNode } from "@/lib/types";

interface FileTreeProps {
  nodes: FileTreeNode[];
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}

export function FileTree({ nodes, onFileSelect, selectedFileId }: FileTreeProps) {
  return (
    <div className="text-[12px]" data-testid="file-tree">
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
    const childCount = node.children?.length || 0;
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center gap-1.5 rounded-md px-2 py-[3px] hover:bg-accent/60 transition-colors group"
          style={{ paddingLeft: `${depth * 14 + 6}px` }}
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground/60 transition-transform duration-150",
              expanded && "rotate-90",
            )}
          />
          {expanded ? (
            <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500/80" />
          ) : (
            <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500/80" />
          )}
          <span className="truncate font-medium text-foreground/80">{node.name}</span>
          <span className="ml-auto text-[9px] tabular-nums text-muted-foreground/50 opacity-0 group-hover:opacity-100 transition-opacity">
            {childCount}
          </span>
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

  // Parse script metadata from file path (lightweight, no content needed)
  const metadata = parseSuiteScriptMetadata(null, node.path);
  const isScript = metadata.scriptType !== "Unknown";

  return (
    <button
      onClick={() => onFileSelect(node.id, node.path)}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-md px-2 py-[3px] transition-colors",
        isSelected
          ? "bg-primary/10 text-foreground font-medium ring-1 ring-primary/20"
          : "hover:bg-accent/60 text-foreground/70 hover:text-foreground",
      )}
      style={{ paddingLeft: `${depth * 14 + 22}px` }}
    >
      {isScript ? (
        <span className={cn(
          "inline-flex items-center justify-center rounded px-1 py-px text-[8px] font-bold leading-none border shrink-0 min-w-[22px]",
          metadata.color,
        )}>
          {metadata.scriptTypeShort}
        </span>
      ) : (
        <File className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
      )}
      <span className="truncate">{node.name}</span>
    </button>
  );
}
