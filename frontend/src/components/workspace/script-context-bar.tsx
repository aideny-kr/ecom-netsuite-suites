"use client";

import {
  Zap, Monitor, Clock, Layers, Layout, Globe,
  RefreshCw, GitBranch, Package, BookOpen, File,
  Link2, Shield, FileCode2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { type ScriptMetadata } from "@/lib/suitescript-parser";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface ScriptContextBarProps {
  metadata: ScriptMetadata;
  filePath: string;
}

const ICON_MAP: Record<string, React.FC<{ className?: string }>> = {
  zap: Zap,
  monitor: Monitor,
  clock: Clock,
  layers: Layers,
  layout: Layout,
  globe: Globe,
  refresh: RefreshCw,
  "git-branch": GitBranch,
  "package": Package,
  book: BookOpen,
  file: File,
  columns: Layout,
};

export function ScriptContextBar({ metadata, filePath }: ScriptContextBarProps) {
  // Don't show for unknown/non-SuiteScript files
  if (metadata.scriptType === "Unknown" && metadata.dependencies.length === 0) {
    return null;
  }

  const IconComponent = ICON_MAP[metadata.icon] || FileCode2;

  return (
    <div className="flex items-center gap-3 border-b bg-muted/20 px-4 py-1 shrink-0 overflow-x-auto">
      {/* Script type badge */}
      <div className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-semibold shrink-0",
        metadata.color,
      )}>
        <IconComponent className="h-3 w-3" />
        {metadata.scriptType === "Unknown" ? "Script" : metadata.scriptType.replace(/Script$/, "")}
      </div>

      {/* API Version */}
      {metadata.apiVersion && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="text-[10px] text-muted-foreground font-mono shrink-0">
              v{metadata.apiVersion}
            </span>
          </TooltipTrigger>
          <TooltipContent>SuiteScript API Version {metadata.apiVersion}</TooltipContent>
        </Tooltip>
      )}

      {/* Record types */}
      {metadata.recordTypes.length > 0 && (
        <div className="flex items-center gap-1 shrink-0">
          <span className="text-[10px] text-muted-foreground">Records:</span>
          {metadata.recordTypes.map((rt) => (
            <span
              key={rt}
              className="inline-flex items-center rounded bg-accent/60 px-1.5 py-px text-[10px] font-medium"
            >
              {rt}
            </span>
          ))}
        </div>
      )}

      {/* Dependencies */}
      {metadata.dependencies.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <div className="flex items-center gap-1 text-[10px] text-muted-foreground shrink-0 cursor-default">
              <Link2 className="h-3 w-3" />
              <span>{metadata.dependencies.length} deps</span>
            </div>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="max-w-[300px]">
            <p className="font-semibold text-[11px] mb-1">Dependencies</p>
            <div className="flex flex-wrap gap-1">
              {metadata.dependencies.map((dep) => (
                <span
                  key={dep}
                  className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono"
                >
                  {dep}
                </span>
              ))}
            </div>
          </TooltipContent>
        </Tooltip>
      )}

      {/* Governance */}
      {metadata.governanceHint && (
        <Tooltip>
          <TooltipTrigger asChild>
            <div className="flex items-center gap-1 text-[10px] text-muted-foreground shrink-0 cursor-default">
              <Shield className="h-3 w-3" />
              <span>{metadata.governanceHint}</span>
            </div>
          </TooltipTrigger>
          <TooltipContent>
            Governance usage limit per execution
          </TooltipContent>
        </Tooltip>
      )}

      {/* Module scope */}
      {metadata.moduleScope && (
        <span className="text-[10px] text-muted-foreground shrink-0">
          Scope: {metadata.moduleScope}
        </span>
      )}
    </div>
  );
}
