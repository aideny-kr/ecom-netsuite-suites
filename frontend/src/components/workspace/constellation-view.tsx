"use client";

import { useState, useMemo } from "react";
import {
  Zap, Monitor, Clock, Layers, Layout, Globe,
  RefreshCw, GitBranch, Package, BookOpen, File,
  ChevronRight, ChevronDown,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  type ScriptType,
  type ScriptMetadata,
  parseSuiteScriptMetadata,
} from "@/lib/suitescript-parser";
import type { FileTreeNode } from "@/lib/types";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface ConstellationViewProps {
  nodes: FileTreeNode[];
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}

interface GroupedFile {
  id: string;
  path: string;
  name: string;
  metadata: ScriptMetadata;
}

interface ScriptGroup {
  type: ScriptType;
  label: string;
  color: string;
  icon: React.ReactNode;
  files: GroupedFile[];
}

const SCRIPT_TYPE_ICONS: Record<string, React.ReactNode> = {
  zap: <Zap className="h-3.5 w-3.5" />,
  monitor: <Monitor className="h-3.5 w-3.5" />,
  clock: <Clock className="h-3.5 w-3.5" />,
  layers: <Layers className="h-3.5 w-3.5" />,
  layout: <Layout className="h-3.5 w-3.5" />,
  globe: <Globe className="h-3.5 w-3.5" />,
  refresh: <RefreshCw className="h-3.5 w-3.5" />,
  "git-branch": <GitBranch className="h-3.5 w-3.5" />,
  "package": <Package className="h-3.5 w-3.5" />,
  book: <BookOpen className="h-3.5 w-3.5" />,
  file: <File className="h-3.5 w-3.5" />,
  columns: <Layout className="h-3.5 w-3.5" />,
};

const GROUP_ORDER: ScriptType[] = [
  "UserEventScript", "ClientScript", "ScheduledScript", "MapReduceScript",
  "Suitelet", "Restlet", "MassUpdateScript", "WorkflowActionScript",
  "Library", "Unknown",
];

function flattenFiles(nodes: FileTreeNode[]): Array<{ id: string; path: string; name: string }> {
  const result: Array<{ id: string; path: string; name: string }> = [];
  function walk(list: FileTreeNode[]) {
    for (const node of list) {
      if (node.is_directory) {
        if (node.children) walk(node.children);
      } else {
        result.push({ id: node.id, path: node.path, name: node.name });
      }
    }
  }
  walk(nodes);
  return result;
}

function getGroupLabel(type: ScriptType): string {
  const labels: Record<ScriptType, string> = {
    UserEventScript: "User Event Scripts",
    ClientScript: "Client Scripts",
    ScheduledScript: "Scheduled Scripts",
    MapReduceScript: "Map/Reduce Scripts",
    Suitelet: "Suitelets",
    Restlet: "RESTlets",
    MassUpdateScript: "Mass Update Scripts",
    WorkflowActionScript: "Workflow Actions",
    BundleInstallationScript: "Bundle Installation",
    Portlet: "Portlets",
    Library: "Libraries & Utilities",
    Unknown: "Other Files",
  };
  return labels[type] || type;
}

export function ConstellationView({ nodes, onFileSelect, selectedFileId }: ConstellationViewProps) {
  const [expandedGroups, setExpandedGroups] = useState<Set<ScriptType>>(
    () => new Set(GROUP_ORDER),
  );

  const groups = useMemo(() => {
    const files = flattenFiles(nodes);
    const groupMap = new Map<ScriptType, GroupedFile[]>();

    for (const file of files) {
      // Parse from path only (no content available in tree view)
      const metadata = parseSuiteScriptMetadata(null, file.path);
      const type = metadata.scriptType;

      if (!groupMap.has(type)) {
        groupMap.set(type, []);
      }
      groupMap.get(type)!.push({
        id: file.id,
        path: file.path,
        name: file.name,
        metadata,
      });
    }

    // Build ordered groups
    const result: ScriptGroup[] = [];
    for (const type of GROUP_ORDER) {
      const files = groupMap.get(type);
      if (!files || files.length === 0) continue;

      const firstMeta = files[0].metadata;
      result.push({
        type,
        label: getGroupLabel(type),
        color: firstMeta.color,
        icon: SCRIPT_TYPE_ICONS[firstMeta.icon] || SCRIPT_TYPE_ICONS.file,
        files: files.sort((a, b) => a.name.localeCompare(b.name)),
      });
    }

    // Add any types not in GROUP_ORDER
    groupMap.forEach((files, type) => {
      if (!GROUP_ORDER.includes(type) && files.length > 0) {
        const firstMeta = files[0].metadata;
        result.push({
          type,
          label: getGroupLabel(type),
          color: firstMeta.color,
          icon: SCRIPT_TYPE_ICONS[firstMeta.icon] || SCRIPT_TYPE_ICONS.file,
          files: files.sort((a: GroupedFile, b: GroupedFile) => a.name.localeCompare(b.name)),
        });
      }
    });

    return result;
  }, [nodes]);

  const toggleGroup = (type: ScriptType) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  if (groups.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-[12px] text-muted-foreground">
        No scripts found
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {groups.map((group) => {
        const isExpanded = expandedGroups.has(group.type);

        return (
          <div key={group.type}>
            {/* Group header */}
            <button
              onClick={() => toggleGroup(group.type)}
              className="flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left hover:bg-accent/50 transition-colors"
            >
              {isExpanded ? (
                <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
              )}
              <span className={cn("shrink-0", group.color)}>
                {group.icon}
              </span>
              <span className="flex-1 truncate text-[11px] font-semibold">
                {group.label}
              </span>
              <span className="text-[10px] tabular-nums text-muted-foreground">
                {group.files.length}
              </span>
            </button>

            {/* Group files */}
            {isExpanded && (
              <div className="ml-3 border-l border-border/50 pl-1.5 space-y-px">
                {group.files.map((file) => (
                  <Tooltip key={file.id}>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => onFileSelect(file.id, file.path)}
                        className={cn(
                          "flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left transition-colors",
                          selectedFileId === file.id
                            ? "bg-accent text-accent-foreground"
                            : "hover:bg-accent/50 text-foreground/80",
                        )}
                      >
                        <span className={cn(
                          "inline-flex items-center justify-center rounded px-1 py-px text-[9px] font-bold leading-none border shrink-0",
                          file.metadata.color,
                        )}>
                          {file.metadata.scriptTypeShort}
                        </span>
                        <span className="truncate text-[12px]">
                          {file.name}
                        </span>
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-[280px]">
                      <div className="space-y-1">
                        <p className="font-mono text-[11px] font-medium">{file.path}</p>
                        <div className="flex flex-wrap gap-1">
                          <span className={cn(
                            "inline-flex items-center rounded px-1.5 py-px text-[10px] font-semibold border",
                            file.metadata.color,
                          )}>
                            {file.metadata.scriptType === "Unknown" ? "Unknown Type" : file.metadata.scriptType}
                          </span>
                          {file.metadata.recordTypes.map((rt) => (
                            <span
                              key={rt}
                              className="inline-flex items-center rounded bg-muted px-1.5 py-px text-[10px] text-muted-foreground"
                            >
                              {rt}
                            </span>
                          ))}
                        </div>
                        {file.metadata.governanceHint && (
                          <p className="text-[10px] text-muted-foreground">
                            Governance: {file.metadata.governanceHint}
                          </p>
                        )}
                      </div>
                    </TooltipContent>
                  </Tooltip>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
