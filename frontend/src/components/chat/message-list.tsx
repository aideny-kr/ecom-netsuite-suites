"use client";

import React, { useEffect, useLayoutEffect, useRef, useState, useMemo, memo, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";
import { cn } from "@/lib/utils";
import { useBranding } from "@/providers/branding-provider";
import type { ChatMessage } from "@/lib/types";
import type { FinancialReportData, DataTableData, TaskOutputData, StreamBlock } from "@/lib/chat-stream";
import type { AgentSummary } from "@/hooks/use-agents";
import type { ChartData } from "@/lib/types";
import { FinancialReport } from "@/components/chat/financial-report";
import { DataFrameTable } from "@/components/chat/data-frame-table";
import { ChartRenderer } from "@/components/chat/chart-renderer";
import { ToolCallStepCard } from "@/components/chat/tool-call-step";
import { ChangeProposalCard } from "@/components/chat/change-proposal-card";
import { WorkspaceToolCard } from "@/components/chat/workspace-tool-card";
import { SuiteQLToolCard } from "@/components/chat/suiteql-tool-card";
import { TaskOutputCard } from "@/components/chat/task-output-card";
import { AgentChatHeader } from "@/components/chat/agent-chat-header";
import { PricingConfigSection } from "@/components/settings/pricing-config-section";
import { InstructionPanel } from "@/components/chat/instruction-panel";
import { TemplateSlot } from "@/components/chat/template-slot";
import { useAgentInstructions, useUpdateAgentInstructions } from "@/hooks/use-agent-instructions";
import { FileCode, Bookmark, Check, Loader2, Copy, ThumbsUp, ThumbsDown, User, Zap } from "lucide-react";
import { ConfidenceBadge } from "@/components/chat/confidence-badge";
import { ImportanceBanner } from "@/components/chat/importance-banner";
import { useChatFeedback } from "@/hooks/use-chat-feedback";
import { StreamingToolCard } from "@/components/chat/streaming-tool-card";
import { DisclosureFooter } from "@/components/chat/disclosure-footer";
import type { DisclosureBlock } from "@/lib/types";

/** Framework-inspired gear/module icon used as AI assistant avatar.
 *  A square with notches on each side — resembles the Framework Computer logo. */
function FrameworkIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M8 1a1 1 0 0 0-1 1v2H4a2 2 0 0 0-2 2v1a1 1 0 0 0 1 1h2v8H3a1 1 0 0 0-1 1v1a2 2 0 0 0 2 2h3v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-2h2v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-2h3a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1h-2V8h2a1 1 0 0 0 1-1V6a2 2 0 0 0-2-2h-3V2a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v2h-2V2a1 1 0 0 0-1-1H8zm1 7a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1H9z"
      />
    </svg>
  );
}

/** Shared markdown components with syntax-highlighted code blocks */
function makeMdComponents(isTerminal: boolean): Components {
  return {
    code({ className, children, ...props }) {
      const match = /language-(\w+)/.exec(className || "");
      const codeString = String(children).replace(/\n$/, "");
      const isMultiline = codeString.includes("\n");

      // Inline code (single line, no language tag)
      if (!match && !isMultiline) {
        return (
          <code
            className="rounded bg-muted px-1.5 py-0.5 text-[13px] font-mono text-foreground"
            {...props}
          >
            {children}
          </code>
        );
      }

      const language = match?.[1] || "text";

      return (
        <div className={cn(
          "group relative my-0 overflow-hidden border border-border/50",
          isTerminal ? "rounded-sm" : "rounded-xl",
        )}>
          <div className="flex items-center justify-between bg-muted/80 px-3 py-1.5 text-[11px] font-medium text-muted-foreground">
            <span>{language}</span>
            <button
              onClick={() => navigator.clipboard.writeText(codeString)}
              className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1 hover:text-foreground"
            >
              <Copy className="h-3 w-3" />
              Copy
            </button>
          </div>
          <div className="overflow-x-auto scrollbar-thin">
            <SyntaxHighlighter
              style={oneDark}
              language={language}
              PreTag="div"
              customStyle={{
                margin: 0,
                borderRadius: 0,
                fontSize: "13px",
                lineHeight: "1.5",
              }}
            >
              {codeString}
            </SyntaxHighlighter>
          </div>
        </div>
      );
    },
  };
}

/** Static default markdown components (no terminal styling) */
const mdComponents: Components = makeMdComponents(false);
const mdComponentsTerminal: Components = makeMdComponents(true);

/**
 * Memoized markdown renderer for streaming text.
 * Re-renders only when content grows by 30+ chars or gains a new line.
 * This replaces the old <pre> tag that showed raw markdown during streaming.
 */
const StreamingMarkdownBlock = memo(
  function StreamingMarkdownBlock({ content, isTerminal }: { content: string; isTerminal: boolean }) {
    return (
      <div className="text-[15px] leading-relaxed prose prose-sm dark:prose-invert max-w-none">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={isTerminal ? mdComponentsTerminal : mdComponents}
        >
          {content}
        </ReactMarkdown>
      </div>
    );
  },
  (prev, next) => {
    // Re-render on every content change for fluid streaming
    return prev.content === next.content;
  }
);

function renderWithMentions(
  content: string,
  onMentionClick?: (filePath: string) => void,
): React.ReactNode[] {
  const mentionRegex = /@workspace:([^\s]+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match;

  while ((match = mentionRegex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      parts.push(content.slice(lastIndex, match.index));
    }
    const filePath = match[1];
    parts.push(
      <button
        key={match.index}
        onClick={() => onMentionClick?.(filePath)}
        className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[12px] font-medium text-primary hover:bg-primary/20 cursor-pointer transition-colors"
        title={`Open ${filePath} in workspace`}
      >
        <FileCode className="h-3 w-3" />
        {filePath.split("/").pop()}
      </button>,
    );
    lastIndex = mentionRegex.lastIndex;
  }

  if (lastIndex < content.length) {
    parts.push(content.slice(lastIndex));
  }

  return parts.length > 0 ? parts : [content];
}

/** Shared pattern for matching closed thinking/reasoning XML blocks. Must create new RegExp for each use (stateful /g flag). */
const THINKING_TAG_PATTERN = String.raw`<(?:thinking|reasoning)>([\s\S]*?)<\/(?:thinking|reasoning)>`;

function parseThinkingBlocks(content: string): Array<{
  type: "text" | "thinking";
  content: string;
}> {
  const parts: Array<{ type: "text" | "thinking"; content: string }> = [];
  const regex = new RegExp(THINKING_TAG_PATTERN, "g");
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) parts.push({ type: "text", content: text });
    }
    parts.push({ type: "thinking", content: match[1].trim() });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < content.length) {
    const text = content.slice(lastIndex).trim();
    if (text) parts.push({ type: "text", content: text });
  }

  return parts.length > 0 ? parts : [{ type: "text", content }];
}

/**
 * Parse streaming content to separate thinking/reasoning blocks from text.
 * Handles incomplete (still-open) tags during streaming.
 */
export function parseStreamingThinking(content: string): {
  thinking: string | null;
  isThinking: boolean;
  text: string;
} {
  const closedRegex = new RegExp(THINKING_TAG_PATTERN, "g");
  let lastThinking: string | null = null;
  let lastIndex = 0;
  let match;
  const textParts: string[] = [];

  while ((match = closedRegex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) textParts.push(text);
    }
    lastThinking = match[1].trim();
    lastIndex = closedRegex.lastIndex;
  }

  // Check remainder after all closed tags
  const remainder = content.slice(lastIndex);

  // Fast bailout: no < means no tags to parse
  if (!remainder.includes("<")) {
    if (lastIndex > 0) {
      const remainingText = remainder.trim();
      if (remainingText) textParts.push(remainingText);
      return { thinking: lastThinking, isThinking: false, text: textParts.join("\n\n") };
    }
    return { thinking: null, isThinking: false, text: content };
  }

  // Check for unclosed open tag
  const openTagMatch = remainder.match(/<(thinking|reasoning)>([\s\S]*)$/);
  if (openTagMatch) {
    const beforeOpenTag = remainder.slice(0, openTagMatch.index).trim();
    if (beforeOpenTag) textParts.push(beforeOpenTag);
    return {
      thinking: openTagMatch[2].trim() || null,
      isThinking: true,
      text: textParts.join("\n\n"),
    };
  }

  // Detect partial opening tags at end of stream (e.g. "<thi", "<thinking")
  // Strip them from visible text so they don't flash to user
  const _PARTIAL_TAGS = ["thinking", "reasoning"];
  const trailingLt = remainder.lastIndexOf("<");
  if (trailingLt >= 0) {
    const tail = remainder.slice(trailingLt);
    const isPartial = _PARTIAL_TAGS.some((tag) => {
      const full = `<${tag}>`;
      return full.startsWith(tail) && tail.length < full.length;
    });
    if (isPartial) {
      const beforePartial = remainder.slice(0, trailingLt).trim();
      if (lastIndex > 0) {
        if (beforePartial) textParts.push(beforePartial);
        return { thinking: lastThinking, isThinking: false, text: textParts.join("\n\n") };
      }
      return { thinking: null, isThinking: false, text: beforePartial || "" };
    }
  }

  if (lastIndex > 0) {
    const remainingText = remainder.trim();
    if (remainingText) textParts.push(remainingText);
    return {
      thinking: lastThinking,
      isThinking: false,
      text: textParts.join("\n\n"),
    };
  }

  return { thinking: null, isThinking: false, text: content };
}

type MarkdownDisplayBlock = {
  kind: "bubble" | "rich";
  content: string;
};

const TABLE_SEPARATOR_PATTERN = /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?\|?\s*$/;

function splitMarkdownDisplayBlocks(content: string): MarkdownDisplayBlock[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownDisplayBlock[] = [];
  let narrativeBuffer: string[] = [];
  let index = 0;

  const flushNarrative = () => {
    const narrative = narrativeBuffer.join("\n").trim();
    if (narrative) {
      blocks.push({ kind: "bubble", content: narrative });
    }
    narrativeBuffer = [];
  };

  while (index < lines.length) {
    const line = lines[index];
    const fence = getFence(line);
    if (fence) {
      flushNarrative();
      const codeLines = [line];
      index += 1;
      while (index < lines.length) {
        codeLines.push(lines[index]);
        if (isFenceClose(lines[index], fence)) {
          index += 1;
          break;
        }
        index += 1;
      }
      blocks.push({ kind: "rich", content: codeLines.join("\n").trim() });
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      flushNarrative();
      const tableLines = [lines[index], lines[index + 1]];
      index += 2;
      while (index < lines.length) {
        const nextLine = lines[index];
        if (!nextLine.trim() || !nextLine.includes("|")) {
          break;
        }
        tableLines.push(nextLine);
        index += 1;
      }
      blocks.push({ kind: "rich", content: tableLines.join("\n").trim() });
      continue;
    }

    if (isIndentedCodeStart(lines, index)) {
      flushNarrative();
      const codeLines = [line];
      index += 1;
      while (
        index < lines.length &&
        (isIndentedCodeLine(lines[index]) || lines[index].trim() === "")
      ) {
        codeLines.push(lines[index]);
        index += 1;
      }
      blocks.push({ kind: "rich", content: codeLines.join("\n").trimEnd() });
      continue;
    }

    narrativeBuffer.push(line);
    index += 1;
  }

  flushNarrative();
  return blocks.length > 0 ? blocks : [{ kind: "bubble", content }];
}

function getFence(line: string): { char: "`" | "~"; len: number } | null {
  const match = line.match(/^\s*(`{3,}|~{3,})/);
  if (!match) return null;
  const fenceText = match[1];
  return {
    char: fenceText[0] as "`" | "~",
    len: fenceText.length,
  };
}

function isFenceClose(line: string, fence: { char: "`" | "~"; len: number }): boolean {
  const match = line.match(/^\s*(`{3,}|~{3,})\s*$/);
  return !!match && match[1][0] === fence.char && match[1].length >= fence.len;
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  return (
    index + 1 < lines.length &&
    lines[index].includes("|") &&
    TABLE_SEPARATOR_PATTERN.test(lines[index + 1])
  );
}

function isIndentedCodeLine(line: string): boolean {
  return /^(?: {4}|\t)/.test(line);
}

function isIndentedCodeStart(lines: string[], index: number): boolean {
  return isIndentedCodeLine(lines[index]) && (index === 0 || lines[index - 1].trim() === "");
}

function MarkdownRenderer({
  content,
  className,
  isTerminal = false,
}: {
  content: string;
  className?: string;
  isTerminal?: boolean;
}) {
  const components = isTerminal ? mdComponentsTerminal : mdComponents;
  return (
    <div className={cn("chat-markdown text-[14px] leading-relaxed", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}


function AssistantNarrativeBubble({ content, isTerminal = false }: { content: string; isTerminal?: boolean }) {
  return (
    <div className={cn(
      isTerminal
        ? "max-w-full bg-[var(--card)] border border-[var(--chat-surface-mid)] shadow-sm p-8 rounded-sm shadow-[0_20px_40px_rgba(255,102,0,0.04)] relative overflow-hidden md:max-w-[75%]"
        : "max-w-full rounded-2xl bg-muted/60 px-4 py-3 md:max-w-[75%]",
    )}>
      {isTerminal && (
        <div className="absolute top-0 left-0 w-1 h-full bg-[var(--chat-accent)]" />
      )}
      <MarkdownRenderer content={content} isTerminal={isTerminal} />
    </div>
  );
}

function AssistantRichBlock({ content, isTerminal = false }: { content: string; isTerminal?: boolean }) {
  return (
    <div
      className={cn(
        "w-full overflow-hidden",
        isTerminal
          ? "rounded-sm border border-[var(--chat-surface-mid)] bg-[var(--chat-surface)]"
          : "rounded-2xl border border-border/60 bg-background/80",
      )}
      data-testid="assistant-rich-block"
    >
      <div className="max-h-[60vh] overflow-auto p-4 scrollbar-thin">
        <MarkdownRenderer content={content} className="chat-markdown-rich" isTerminal={isTerminal} />
      </div>
    </div>
  );
}

function AssistantTextBlocks({ content, isTerminal = false }: { content: string; isTerminal?: boolean }) {
  const blocks = splitMarkdownDisplayBlocks(content);

  return (
    <>
      {blocks.map((block, index) =>
        block.kind === "rich" ? (
          <AssistantRichBlock key={index} content={block.content} isTerminal={isTerminal} />
        ) : (
          <AssistantNarrativeBubble key={index} content={block.content} isTerminal={isTerminal} />
        ),
      )}
    </>
  );
}

/** Agent display config for indicator badges */
const AGENT_TAGS: Record<string, { label: string; color: string }> = {
  "bi-agent": { label: "BI Analyst", color: "text-violet-500 bg-violet-500/10" },
  "recon-agent": { label: "Reconciliation", color: "text-emerald-500 bg-emerald-500/10" },
  "pricing-agent": { label: "Pricing", color: "text-amber-500 bg-amber-500/10" },
};

/** Status headline above thinking — shows what the agent is doing right now */
export function StatusHeadline({ steps, isTerminal = false }: { steps: { label: string; status: "complete" | "running" }[]; isTerminal?: boolean }) {
  const display = useMemo(() => {
    if (steps.length === 0) return null;
    return steps.findLast((s) => s.status === "running") || steps.findLast((s) => s.status === "complete") || null;
  }, [steps]);

  if (!display) return null;

  const isRunning = display.status === "running";

  return (
    <div className={cn(
      "flex items-center gap-2 text-[13px] font-medium",
      isRunning ? "text-foreground" : "text-muted-foreground",
    )}>
      {isRunning ? (
        <span className={cn(
          "h-1.5 w-1.5 rounded-full animate-pulse",
          isTerminal ? "bg-[var(--chat-accent)]" : "bg-primary",
        )} />
      ) : (
        <Check className="h-3.5 w-3.5 text-emerald-500" />
      )}
      <span className={isTerminal ? "tracking-wider uppercase text-[11px]" : ""}>
        {display.label}
      </span>
    </div>
  );
}

/** Collapsed thinking block for completed messages */
export function ThinkingBlock({ content, isTerminal = false }: { content: string; isTerminal?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <details className={cn(
      "mb-2 text-[12px] group",
      isTerminal
        ? "rounded-sm border border-[var(--chat-surface-mid)] bg-[var(--chat-surface-variant)]/50"
        : "rounded-lg border border-muted/50 bg-muted/20",
    )}>
      <summary className="cursor-pointer select-none px-3 py-2 text-muted-foreground/60 hover:text-muted-foreground font-medium flex items-center gap-2 transition-colors">
        <FrameworkIcon className="h-3 w-3" />
        Thought process
      </summary>
      <div className="relative">
        <div
          data-testid="thinking-content"
          className={cn(
            "px-3 pb-2.5 text-muted-foreground/70 text-[12px] leading-relaxed overflow-hidden transition-all duration-200",
            !expanded && "max-h-[3.5rem]",
          )}
        >
          <MarkdownRenderer content={content} className="text-[12px] text-muted-foreground/70" isTerminal={isTerminal} />
        </div>
        {!expanded && (
          <div data-testid="thinking-fade" className="thinking-fade-overlay absolute bottom-6 left-0 right-0 h-6 pointer-events-none" />
        )}
        <button
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? "Collapse" : "Show more"}
          className="w-full px-3 pb-2 pt-0.5 text-[11px] font-medium text-muted-foreground/50 hover:text-muted-foreground transition-colors text-left"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      </div>
    </details>
  );
}

/** Live thinking block shown during streaming — Gemini-style animation */
export function StreamingThinkingBlock({ content, isActive, isTerminal = false }: { content: string | null; isActive: boolean; isTerminal?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={cn(
      "mb-2 overflow-hidden",
      isTerminal
        ? "rounded-sm border border-[var(--chat-surface-mid)] bg-[var(--chat-surface-variant)]/50"
        : "rounded-lg border border-primary/10 bg-primary/[0.03]",
    )}>
      <div className="flex items-center gap-2 px-3 py-2">
        {isActive && (
          <span className="relative flex h-2 w-2">
            <span className={cn(
              "absolute inline-flex h-full w-full animate-ping rounded-full",
              isTerminal ? "bg-[var(--chat-accent)]/40" : "bg-primary/40",
            )} />
            <span className={cn(
              "relative inline-flex h-2 w-2 rounded-full",
              isTerminal ? "bg-[var(--chat-accent)]/60" : "bg-primary/60",
            )} />
          </span>
        )}
        <span className={cn(
          "text-[12px] font-medium",
          isTerminal ? "text-[var(--chat-accent)]/70" : "text-primary/70",
        )}>
          {isActive ? "Thinking..." : "Thought process"}
        </span>
      </div>
      {content && (
        <div className="relative">
          <div
            data-testid="thinking-content"
            className={cn(
              "px-3 text-[12px] leading-relaxed text-muted-foreground/60 overflow-hidden transition-all duration-200",
              isActive && "animate-thinking-fade",
              !expanded && "max-h-[3.5rem]",
            )}
          >
            {/* Use plain text while actively streaming to avoid expensive ReactMarkdown re-renders per chunk */}
            {isActive ? (
              <p className="whitespace-pre-wrap">{content}</p>
            ) : (
              <MarkdownRenderer content={content} className="text-[12px] text-muted-foreground/60" isTerminal={isTerminal} />
            )}
          </div>
          {!expanded && (
            <div data-testid="thinking-fade" className="thinking-fade-overlay absolute bottom-6 left-0 right-0 h-6 pointer-events-none" />
          )}
          <button
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? "Collapse" : "Expand"}
            className="w-full px-3 pb-2 pt-0.5 text-[11px] font-medium text-muted-foreground/40 hover:text-muted-foreground/70 transition-colors text-left"
          >
            {expanded ? "Show less" : "Show more"}
          </button>
        </div>
      )}
    </div>
  );
}

interface MessageListProps {
  messages: ChatMessage[];
  isLoading: boolean;
  pendingUserMessage?: string | null;
  isWaitingForReply?: boolean;
  onMentionClick?: (filePath: string) => void;
  workspaceId?: string | null;
  onViewDiff?: (changesetId: string) => void;
  onChangesetAction?: () => void;
  streamBlocks?: StreamBlock[];
  streamingMessage?: ChatMessage | null;
  financialReports?: Map<string, FinancialReportData>;
  dataTables?: Map<string, DataTableData>;
  chartsByMessage?: Map<string, ChartData[]>;
  taskOutputs?: Map<string, TaskOutputData>;
  disclosures?: Map<string, DisclosureBlock>;
  streamingDisclosure?: DisclosureBlock | null;
  pinnedAgentId?: string | null;
  agents?: AgentSummary[];
  agentTab?: "chat" | "config";
  onTabChange?: (tab: "chat" | "config") => void;
  onTemplateUploaded?: (file: { id: string; filename: string }) => void;
  onRemoveTemplate?: () => void;
  templateFile?: { id: string; filename: string } | null;
  onImportanceOverride?: (messageId: string, newTier: number) => void;
  variant?: "default" | "terminal";
}

export function MessageList({
  messages,
  isLoading,
  pendingUserMessage,
  isWaitingForReply,
  onMentionClick,
  workspaceId,
  onViewDiff,
  onChangesetAction,
  streamBlocks = [],
  streamingMessage,
  financialReports,
  dataTables,
  chartsByMessage,
  taskOutputs,
  disclosures,
  streamingDisclosure = null,
  pinnedAgentId,
  agents,
  agentTab,
  onTabChange,
  onTemplateUploaded,
  onRemoveTemplate,
  templateFile,
  onImportanceOverride,
  variant,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const isTerminal = variant === "terminal";
  const { brandName } = useBranding();
  const { data: agentInstructions } = useAgentInstructions(pinnedAgentId ?? null);
  const updateInstructions = useUpdateAgentInstructions(pinnedAgentId ?? "");

  // Track if user has scrolled up (should NOT auto-scroll)
  const shouldAutoScrollRef = useRef(true);
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom < 80;
  }, []);

  // Wheel up immediately breaks auto-scroll lock (don't wait for threshold)
  const handleWheel = useCallback((e: React.WheelEvent) => {
    if (e.deltaY < 0) {
      shouldAutoScrollRef.current = false;
    }
  }, []);

  // Synchronous scroll — fires BEFORE browser paint, prevents visible jump
  useLayoutEffect(() => {
    if (shouldAutoScrollRef.current && bottomRef.current) {
      bottomRef.current.scrollIntoView({ block: "end" });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, pendingUserMessage, isWaitingForReply]);

  // ResizeObserver to catch streaming content growth between React renders
  // Uses 50ms debounce to smooth scroll updates and prevent bounce
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let scrollTimerId: ReturnType<typeof setTimeout> | null = null;

    const observer = new ResizeObserver(() => {
      if (scrollTimerId) clearTimeout(scrollTimerId);
      scrollTimerId = setTimeout(() => {
        if (shouldAutoScrollRef.current) {
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        }
        scrollTimerId = null;
      }, 50);
    });

    Array.from(el.children).forEach((child) => observer.observe(child));
    const mo = new MutationObserver(() => {
      Array.from(el.children).forEach((child) => observer.observe(child));
      if (scrollTimerId) clearTimeout(scrollTimerId);
      scrollTimerId = setTimeout(() => {
        if (shouldAutoScrollRef.current) {
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        }
        scrollTimerId = null;
      }, 50);
    });
    mo.observe(el, { childList: true });
    return () => {
      observer.disconnect();
      mo.disconnect();
      if (scrollTimerId) clearTimeout(scrollTimerId);
    };
  }, []);

  // Skip loading indicator if we have a pending message (just created session)
  if (isLoading && !pendingUserMessage && !isWaitingForReply) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span className="text-[13px] text-muted-foreground">
            Loading conversation...
          </span>
        </div>
      </div>
    );
  }

  if (messages.length === 0 && !pendingUserMessage) {
    // Agent workspace empty state — show header + panels + agent-specific prompt
    if (pinnedAgentId) {
      const agentName = agents?.find(a => a.agent_id === pinnedAgentId)?.display_name || pinnedAgentId;
      return (
        <div className="flex h-full flex-col">
          <AgentChatHeader
            agentId={pinnedAgentId}
            agentName={agentName}
            activeTab={agentTab || "chat"}
            onTabChange={(tab) => onTabChange?.(tab)}
          />
          {(!agentTab || agentTab === "chat") ? (
            <>
              <div className="px-6 py-3 space-y-2 border-b border-border/50">
                <InstructionPanel
                  agentId={pinnedAgentId}
                  instructions={agentInstructions?.instructions || ""}
                  canEdit={true}
                  onSave={(text) => updateInstructions.mutate(text)}
                  lastUpdated={agentInstructions?.updated_at || undefined}
                />
                <TemplateSlot
                  template={templateFile ? { id: templateFile.id, name: templateFile.filename, size: 0 } : null}
                  onUpload={(f) => onTemplateUploaded?.({ id: f.id, filename: f.name })}
                  onRemove={() => onRemoveTemplate?.()}
                />
              </div>
              <div className="flex-1 flex items-center justify-center">
                <div className="text-center space-y-3">
                  <p className="text-muted-foreground text-[14px]">
                    {templateFile
                      ? `Ready to process "${templateFile.filename}" — type a command or say "convert prices"`
                      : `Upload a template or ask ${agentName} anything`}
                  </p>
                </div>
              </div>
            </>
          ) : (
            <div className="flex-1 overflow-auto px-8 py-6">
              <PricingConfigSection />
            </div>
          )}
        </div>
      );
    }

    if (isTerminal) {
      return (
        <div className="flex h-full items-start px-0 py-4">
          <div className="max-w-4xl">
            <h1 className="font-headline font-black text-[3.5rem] leading-none -tracking-[0.02em] text-foreground mb-4">
              {(() => {
                const name = brandName || "Suite Studio AI";
                const aiIndex = name.indexOf("AI");
                if (aiIndex >= 0) {
                  return (
                    <>
                      {name.slice(0, aiIndex)}
                      <span className="text-[var(--chat-accent)]">AI</span>
                      {name.slice(aiIndex + 2)}
                    </>
                  );
                }
                return name;
              })()}
            </h1>
            <p className="text-muted-foreground text-base max-w-xl leading-relaxed">
              Ask questions about your business operations, data, or docs.
            </p>
          </div>
        </div>
      );
    }
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10">
            <FrameworkIcon className="h-6 w-6 text-primary" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">
            How can I help?
          </h3>
          <p className="mt-1.5 max-w-xs text-[14px] leading-relaxed text-muted-foreground">
            Ask questions about your business operations, data, or docs.
          </p>
        </div>
      </div>
    );
  }

  const shouldRenderStreamingMessage = !!(
    streamingMessage &&
    !messages.some((message) => {
      if (message.role !== "assistant") return false;
      if (message.id === streamingMessage.id) return true;
      return (
        message.content === streamingMessage.content &&
        JSON.stringify(message.tool_calls ?? null) ===
          JSON.stringify(streamingMessage.tool_calls ?? null)
      );
    })
  );

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      onWheel={handleWheel}
      className={cn(
        "h-full min-h-0 min-w-0 overflow-auto",
        isTerminal
          ? "px-10 py-8 space-y-8"
          : "px-6 py-6 space-y-5 scrollbar-thin",
      )}
      style={{ scrollbarGutter: "stable" }}
      data-testid="message-list"
    >
      {pinnedAgentId && (
        <div className="mb-4 -mx-10 -mt-8">
          <AgentChatHeader
            agentId={pinnedAgentId}
            agentName={agents?.find(a => a.agent_id === pinnedAgentId)?.display_name || pinnedAgentId}
            activeTab={agentTab || "chat"}
            onTabChange={(tab) => onTabChange?.(tab)}
          />
          {(!agentTab || agentTab === "chat") && (
            <div className="px-6 pb-3 space-y-2 border-b border-border/50">
              <InstructionPanel
                agentId={pinnedAgentId}
                instructions={agentInstructions?.instructions || ""}
                canEdit={true}
                onSave={(text) => updateInstructions.mutate(text)}
                lastUpdated={agentInstructions?.updated_at || undefined}
              />
              <TemplateSlot
                template={templateFile ? { id: templateFile.id, name: templateFile.filename, size: 0 } : null}
                onUpload={(f) => onTemplateUploaded?.({ id: f.id, filename: f.name })}
                onRemove={() => onRemoveTemplate?.()}
              />
            </div>
          )}
          {agentTab === "config" && (
            <div className="flex-1 overflow-auto px-8 py-6">
              <PricingConfigSection />
            </div>
          )}
        </div>
      )}
      {messages.map((message) => (
        message.role === "assistant" ? (
          <AssistantMessageRow
            key={message.id}
            message={message}
            messages={messages}
            workspaceId={workspaceId}
            onViewDiff={onViewDiff}
            onChangesetAction={onChangesetAction}
            onImportanceOverride={onImportanceOverride}
            financialReportData={financialReports?.get(message.id) ?? null}
            dataTableData={dataTables?.get(message.id) ?? null}
            chartDataList={chartsByMessage?.get(message.id) ?? null}
            taskOutputData={taskOutputs?.get(message.id) ?? null}
            disclosureData={disclosures?.get(message.id) ?? null}
            isTerminal={isTerminal}
          />
        ) : isTerminal ? (
          <div key={message.id} className="flex max-w-full justify-end gap-4">
            <div className="max-w-full bg-[var(--chat-surface-low)] p-6 rounded-sm border border-[var(--chat-surface-variant)] relative overflow-hidden md:max-w-[75%]">
              <div className="absolute top-0 right-0 w-1 h-full bg-muted-foreground/40" />
              <p className="text-[14px] leading-relaxed whitespace-pre-wrap break-words text-foreground">
                {renderWithMentions(message.content, onMentionClick)}
              </p>
            </div>
            <div className="w-10 h-10 bg-[var(--chat-surface-high)] flex-shrink-0 flex items-center justify-center border border-[var(--chat-surface-mid)]">
              <User className="h-4 w-4 text-muted-foreground" />
            </div>
          </div>
        ) : (
          <div key={message.id} className="flex max-w-full justify-end gap-3">
            <div className="max-w-full rounded-2xl bg-primary px-4 py-2.5 text-primary-foreground md:max-w-[75%]">
              <p className="text-[14px] leading-relaxed whitespace-pre-wrap break-words">
                {renderWithMentions(message.content, onMentionClick)}
              </p>
            </div>
          </div>
        )
      ))}

      {/* Optimistic pending user message */}
      {pendingUserMessage && (
        isTerminal ? (
          <div className="flex max-w-full justify-end gap-4">
            <div className="max-w-full bg-[var(--chat-surface-low)] p-6 rounded-sm border border-[var(--chat-surface-variant)] relative overflow-hidden md:max-w-[75%]">
              <div className="absolute top-0 right-0 w-1 h-full bg-muted-foreground/40" />
              <p className="text-[14px] leading-relaxed whitespace-pre-wrap text-foreground">
                {pendingUserMessage}
              </p>
            </div>
            <div className="w-10 h-10 bg-[var(--chat-surface-high)] flex-shrink-0 flex items-center justify-center border border-[var(--chat-surface-mid)]">
              <User className="h-4 w-4 text-muted-foreground" />
            </div>
          </div>
        ) : (
          <div className="flex max-w-full justify-end gap-3">
            <div className="max-w-full rounded-2xl bg-primary px-4 py-2.5 text-primary-foreground md:max-w-[75%]">
              <p className="text-[14px] leading-relaxed whitespace-pre-wrap">
                {pendingUserMessage}
              </p>
            </div>
          </div>
        )
      )}

      {/* Thinking indicator / Streaming */}
      {shouldRenderStreamingMessage && streamingMessage && (
        <AssistantMessageRow
          message={streamingMessage}
          messages={messages}
          workspaceId={workspaceId}
          onViewDiff={onViewDiff}
          onChangesetAction={onChangesetAction}
          isStreamingPreview
          isTerminal={isTerminal}
          disclosureData={streamingDisclosure}
        />
      )}

      {!shouldRenderStreamingMessage && (isWaitingForReply || streamBlocks.length > 0) && (
        <div className="flex min-w-0 justify-start gap-3">
          {isTerminal ? (
            <div className="w-10 h-10 bg-[var(--card)] flex-shrink-0 flex items-center justify-center border border-[var(--chat-surface-mid)]">
              <Zap className="h-4 w-4 text-[var(--chat-accent)]" />
            </div>
          ) : (
            <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10">
              <FrameworkIcon className="h-3.5 w-3.5 text-primary" />
            </div>
          )}
          <div className="min-w-0 flex-1">
            <div className={cn(
              "min-w-0 overflow-hidden",
              isTerminal
                ? "rounded-sm border border-[var(--chat-surface-mid)] bg-[var(--chat-surface)]"
                : "rounded-2xl border border-border/50 bg-muted/40",
            )}>
              <div className="flex min-w-0 flex-col gap-2 px-4 py-3">
            {/* Render blocks in chronological order */}
            {streamBlocks.length > 0 ? (
              streamBlocks.map((block) => {
                switch (block.type) {
                  case "thinking":
                    return (
                      <StreamingThinkingBlock
                        key={block.id}
                        content={block.content}
                        isActive={block.isActive}
                        isTerminal={isTerminal}
                      />
                    );
                  case "text": {
                    const parsed = parseStreamingThinking(block.content);
                    return (
                      <React.Fragment key={block.id}>
                        {(parsed.thinking !== null || parsed.isThinking) && (
                          <StreamingThinkingBlock
                            content={parsed.thinking}
                            isActive={parsed.isThinking}
                            isTerminal={isTerminal}
                          />
                        )}
                        {parsed.text && (
                          <StreamingMarkdownBlock content={parsed.text} isTerminal={isTerminal} />
                        )}
                      </React.Fragment>
                    );
                  }
                  case "tool":
                    return <StreamingToolCard key={block.id} tool={block.tool} isTerminal={isTerminal} />;
                  case "data_table":
                    return (
                      <div key={block.id} className="animate-table-appear">
                        <DataFrameTable data={block.data} queryText={block.data.query} />
                      </div>
                    );
                  case "financial_report":
                    return (
                      <div key={block.id} className="animate-table-appear">
                        <FinancialReport data={block.data} />
                      </div>
                    );
                  case "chart":
                    return (
                      <div key={block.id} className="animate-table-appear">
                        <ChartRenderer data={block.data} />
                      </div>
                    );
                  case "task_output":
                    return (
                      <div key={block.id} className="animate-table-appear">
                        <TaskOutputCard data={block.data} />
                      </div>
                    );
                  default:
                    return null;
                }
              })
            ) : (
              /* Idle spinner when nothing streaming yet */
              isWaitingForReply && (
                isTerminal ? (
                  <div className="flex items-center gap-4">
                    <div className="h-2 w-2 bg-[var(--chat-accent)] animate-pulse" />
                    <span className="text-[10px] tracking-widest text-[var(--chat-accent)] uppercase">
                      PROCESSING...
                    </span>
                  </div>
                ) : (
                  <span className="inline-flex gap-1 h-[20px] items-center">
                    <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:0ms]" />
                    <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:150ms]" />
                    <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:300ms]" />
                  </span>
                )
              )
            )}
              </div>
            </div>
          </div>
        </div>
      )}

      <div ref={bottomRef} style={{ overflowAnchor: "auto" }} />
    </div>
  );
}

const AssistantMessageRow = memo(function AssistantMessageRow({
  message,
  messages,
  workspaceId,
  onViewDiff,
  onChangesetAction,
  isStreamingPreview = false,
  onImportanceOverride,
  financialReportData = null,
  dataTableData = null,
  chartDataList = null,
  taskOutputData = null,
  disclosureData = null,
  isTerminal = false,
}: {
  message: ChatMessage;
  messages: ChatMessage[];
  workspaceId?: string | null;
  onViewDiff?: (changesetId: string) => void;
  onChangesetAction?: () => void;
  isStreamingPreview?: boolean;
  onImportanceOverride?: (messageId: string, newTier: number) => void;
  financialReportData?: FinancialReportData | null;
  dataTableData?: DataTableData | null;
  chartDataList?: ChartData[] | null;
  taskOutputData?: TaskOutputData | null;
  disclosureData?: DisclosureBlock | null;
  isTerminal?: boolean;
}) {
  const { brandName: agentName } = useBranding();
  return (
    <div className="flex min-w-0 justify-start gap-3">
      {isTerminal ? (
        <div className="w-10 h-10 bg-[var(--card)] flex-shrink-0 flex items-center justify-center border border-[var(--chat-surface-mid)]">
          <Zap className="h-4 w-4 text-[var(--chat-accent)]" />
        </div>
      ) : (
        <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10">
          <FrameworkIcon className="h-3.5 w-3.5 text-primary" />
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        {isTerminal && (
          <div className="flex justify-between mb-1">
            <span className="text-[10px] tracking-widest text-[var(--chat-accent)] uppercase font-medium">
              {(agentName || "SUITE_STUDIO").toUpperCase().replace(/\s+/g, "_")} [AGENT]
            </span>
            <span className="text-[10px] tracking-widest text-muted-foreground">
              {new Date(message.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          </div>
        )}

        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className="space-y-1.5">
            {message.tool_calls.map((tc, idx) => {
              if (
                tc.tool === "workspace_propose_patch" &&
                workspaceId &&
                onViewDiff
              ) {
                return (
                  <ChangeProposalCard
                    key={idx}
                    step={tc}
                    workspaceId={workspaceId}
                    onViewDiff={onViewDiff}
                    onChangesetAction={onChangesetAction}
                  />
                );
              }
              if (tc.tool === "netsuite_suiteql" || tc.result_payload?.kind === "table") {
                // Skip SuiteQLToolCard when DataFrameTable is handling the display
                if (dataTableData) return null;
                const msgIndex = messages.indexOf(message);
                const prevUserMsg = messages
                  .slice(0, msgIndex)
                  .reverse()
                  .find((m) => m.role === "user");
                return (
                  <SuiteQLToolCard
                    key={idx}
                    step={tc}
                    userQuestion={prevUserMsg?.content}
                  />
                );
              }
              if (tc.tool.startsWith("workspace_")) {
                return <WorkspaceToolCard key={idx} step={tc} />;
              }
              return <ToolCallStepCard key={idx} step={tc} />;
            })}
          </div>
        )}

        {financialReportData && (
          <FinancialReport data={financialReportData} />
        )}

        {dataTableData && (
          <DataFrameTable data={dataTableData} queryText={dataTableData.query} />
        )}

        {chartDataList && chartDataList.length > 0 && chartDataList.map((chart, idx) => (
          <ChartRenderer key={idx} data={chart} />
        ))}

        {taskOutputData && (
          <TaskOutputCard data={taskOutputData} />
        )}

        {disclosureData && <DisclosureFooter disclosure={disclosureData} />}

        <div className="flex min-w-0 flex-col gap-2">
          {parseThinkingBlocks(message.content).map((part, index) =>
            part.type === "thinking" ? (
              <ThinkingBlock key={index} content={part.content} isTerminal={isTerminal} />
            ) : (
              <AssistantTextBlocks key={index} content={part.content} isTerminal={isTerminal} />
            ),
          )}
        </div>

        {message.citations && message.citations.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-1.5">
            {message.citations.map((citation, idx) => (
              <span
                key={idx}
                className={cn(
                  "inline-flex items-center px-2.5 py-1 text-[11px] font-medium",
                  isTerminal
                    ? "rounded-sm bg-[var(--chat-surface)]"
                    : "rounded-full bg-background/60",
                )}
                title={citation.snippet}
              >
                {citation.type === "doc" ? "\u{1F4C4}" : "\u{1F4CA}"} {citation.title}
              </span>
            ))}
          </div>
        )}

        {message.tool_calls?.some((tc) => tc.tool === "netsuite_suiteql") && (
          <InlineSaveLink
            message={message}
            messages={messages}
          />
        )}

        {!isStreamingPreview && message.query_importance != null && message.query_importance >= 2 && (
          <ImportanceBanner
            tier={message.query_importance}
            messageId={message.id}
            onOverride={onImportanceOverride}
          />
        )}

        {!isStreamingPreview && message.tool_calls && message.tool_calls.length > 0 && (
          <FeedbackButtons message={message} />
        )}

        {!isStreamingPreview && message.model_used && (
          <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
            {message.agent_id && AGENT_TAGS[message.agent_id] && (
              <span className={cn("rounded px-1.5 py-0.5 font-medium", AGENT_TAGS[message.agent_id].color)}>
                {AGENT_TAGS[message.agent_id].label}
              </span>
            )}
            {message.is_byok ? (
              <span className="rounded bg-blue-500/10 px-1.5 py-0.5 font-medium text-blue-600 dark:text-blue-400">
                BYOK
              </span>
            ) : (
              <span className="rounded bg-muted px-1.5 py-0.5 font-medium">
                Platform
              </span>
            )}
            <span>{message.provider_used}</span>
            <span>/</span>
            <span>{message.model_used}</span>
            {message.input_tokens != null && message.output_tokens != null && (
              <>
                <span className="ml-1">·</span>
                <span>{(message.input_tokens + message.output_tokens).toLocaleString()} tokens</span>
              </>
            )}
            {message.confidence_score != null && (
              <>
                <span className="ml-1">·</span>
                <ConfidenceBadge score={message.confidence_score} />
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

/**
 * Subtle inline save link rendered below assistant messages that contain
 * SuiteQL tool calls. Provides a secondary save trigger near the data table.
 */
function InlineSaveLink({
  message,
  messages,
}: {
  message: ChatMessage;
  messages: ChatMessage[];
}) {
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");

  const suiteqlCall = message.tool_calls?.find(
    (tc) => tc.tool === "netsuite_suiteql",
  );
  const queryText = (suiteqlCall?.params?.query as string) ?? "";

  const msgIndex = messages.indexOf(message);
  const prevUserMsg = messages
    .slice(0, msgIndex)
    .reverse()
    .find((m) => m.role === "user");
  const autoName = prevUserMsg?.content?.slice(0, 120) ?? "Saved Query";

  const mutation = useCreateSavedQuery();

  if (!queryText) return null;

  if (state === "saved") {
    return (
      <div className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
        <Check className="h-3 w-3" />
        Saved to Analytics
      </div>
    );
  }

  return (
    <div className="mt-2">
      <button
        onClick={() => {
          setState("saving");
          mutation.mutate(
            { name: autoName, query_text: queryText },
            { onSuccess: () => setState("saved") },
          );
        }}
        disabled={mutation.isPending}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-primary transition-colors"
      >
        {mutation.isPending ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Bookmark className="h-3 w-3" />
        )}
        Save query to Analytics
      </button>
      {mutation.isError && (
        <span className="mt-0.5 text-[11px] text-destructive">
          Failed to save
        </span>
      )}
    </div>
  );
}

function FeedbackButtons({ message }: { message: ChatMessage }) {
  const feedbackMutation = useChatFeedback();
  const [localFeedback, setLocalFeedback] = useState<"helpful" | "not_helpful" | null>(
    message.user_feedback ?? null,
  );

  const feedback = localFeedback ?? message.user_feedback ?? null;
  const hasFeedback = feedback != null;

  const handleClick = (value: "helpful" | "not_helpful") => {
    setLocalFeedback(value);
    feedbackMutation.mutate({ messageId: message.id, feedback: value });
  };

  return (
    <div className="flex items-center gap-1 mt-1">
      <button
        onClick={() => handleClick("helpful")}
        disabled={hasFeedback || feedbackMutation.isPending}
        aria-label="Helpful"
        className={cn(
          "p-1 rounded-md text-muted-foreground/50 transition-colors",
          feedback === "helpful"
            ? "text-emerald-500 bg-emerald-50 dark:bg-emerald-950/30"
            : hasFeedback
              ? "opacity-30 cursor-not-allowed"
              : "hover:text-foreground hover:bg-muted",
        )}
      >
        <ThumbsUp className="h-3.5 w-3.5" />
      </button>
      <button
        onClick={() => handleClick("not_helpful")}
        disabled={hasFeedback || feedbackMutation.isPending}
        aria-label="Not helpful"
        className={cn(
          "p-1 rounded-md text-muted-foreground/50 transition-colors",
          feedback === "not_helpful"
            ? "text-rose-500 bg-rose-50 dark:bg-rose-950/30"
            : hasFeedback
              ? "opacity-30 cursor-not-allowed"
              : "hover:text-foreground hover:bg-muted",
        )}
      >
        <ThumbsDown className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
