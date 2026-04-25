"use client";

import { useState, useCallback, useMemo, useEffect, useRef, type KeyboardEvent } from "react";
import { ArrowUp, ArrowRight, AtSign, X, Slash, Paperclip, FileSpreadsheet, Square } from "lucide-react";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { FileMentionPicker } from "@/components/chat/file-mention-picker";
import { DriveFileMentionPicker } from "@/components/chat/drive-file-mention-picker";
import {
  detectDriveTrigger,
  insertDriveMention,
} from "@/components/chat/drive-mention-trigger";
import { AnalyticsDashboard } from "@/components/analytics/AnalyticsDashboard";
import { apiClient } from "@/lib/api-client";
import type { AgentSkillMetadata } from "@/lib/types";

interface ChatInputProps {
  onSend: (content: string, fileId?: string) => void;
  onStop?: () => void;
  isLoading: boolean;
  isRunning?: boolean;
  workspaceId?: string | null;
  variant?: "default" | "terminal";
}

export function ChatInput({ onSend, onStop, isLoading, isRunning, workspaceId, variant }: ChatInputProps) {
  const isTerminal = variant === "terminal";
  const [value, setValue] = useState("");
  const [attachedFile, setAttachedFile] = useState<{ id: string; name: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [driveMentionOpen, setDriveMentionOpen] = useState(false);
  const [driveMentionQuery, setDriveMentionQuery] = useState("");
  const [commandOpen, setCommandOpen] = useState(false);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Fetch available agent skills
  const { data: agentSkills = [] } = useQuery<AgentSkillMetadata[]>({
    queryKey: ["agent-skills"],
    queryFn: () => apiClient.get<AgentSkillMetadata[]>("/api/v1/skills/catalog"),
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
  });

  // Build command list: agent skills + built-in commands
  const commands = useMemo(() => {
    const items: { trigger: string; name: string; description: string; type: "skill" | "builtin" }[] = [];

    // Agent skills
    for (const skill of agentSkills) {
      const primaryTrigger = skill.triggers.find((t) => t.startsWith("/")) || skill.triggers[0];
      items.push({
        trigger: primaryTrigger,
        name: skill.name,
        description: skill.description,
        type: "skill",
      });
    }

    // Built-in commands
    items.push({
      trigger: "/export_analytics",
      name: "Saved Queries",
      description: "View and manage saved SuiteQL queries",
      type: "builtin",
    });

    return items;
  }, [agentSkills]);

  // Filter commands based on current input
  const filteredCommands = useMemo(() => {
    if (!commandOpen) return [];
    const typed = value.toLowerCase();
    if (typed === "/") return commands;
    return commands.filter(
      (cmd) =>
        cmd.trigger.toLowerCase().startsWith(typed) ||
        cmd.name.toLowerCase().includes(typed.slice(1)),
    );
  }, [commandOpen, value, commands]);

  // Reset selection when filtered list changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [filteredCommands.length]);

  const mentions = useMemo(
    () => Array.from(value.matchAll(/@workspace:([^\s]+)/g)).map((m) => m[1]),
    [value],
  );

  const handleFileUpload = useCallback(async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    const headers: Record<string, string> = {};
    const token = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
    if (token) headers["Authorization"] = `Bearer ${token}`;
    try {
      const res = await fetch(`${baseUrl}/api/v1/task-files/upload`, {
        method: "POST",
        body: formData,
        headers,
        credentials: "include",
      });
      if (res.ok) {
        const data = await res.json();
        setAttachedFile({ id: data.id, name: data.filename });
      }
    } catch {
      // Silently fail — user can retry
    }
  }, []);

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isLoading) return;
    onSend(trimmed, attachedFile?.id || undefined);
    setValue("");
    setAttachedFile(null);
    setCommandOpen(false);
  }, [value, isLoading, onSend, attachedFile]);

  // Track when a command was just selected to prevent popover from reopening
  const commandJustSelected = useRef(false);

  const handleCommandSelect = useCallback(
    (cmd: (typeof commands)[0]) => {
      if (cmd.type === "builtin" && cmd.trigger === "/export_analytics") {
        setCommandOpen(false);
        setValue("");
        setAnalyticsOpen(true);
        return;
      }

      // Agent skill — autocomplete the trigger into the input
      commandJustSelected.current = true;
      setValue(cmd.trigger + " ");
      setCommandOpen(false);
      textareaRef.current?.focus();
    },
    [],
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Command popover navigation
      if (commandOpen && filteredCommands.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % filteredCommands.length);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSelectedIndex((prev) => (prev - 1 + filteredCommands.length) % filteredCommands.length);
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          handleCommandSelect(filteredCommands[selectedIndex]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setCommandOpen(false);
          return;
        }
      }

      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend, commandOpen, filteredCommands, selectedIndex, handleCommandSelect],
  );

  const handleMentionSelect = useCallback(
    (file: { file_id: string; path: string }) => {
      setValue((prev) => {
        const cleaned = prev.endsWith("@") ? prev.slice(0, -1) : prev;
        return `${cleaned}@workspace:${file.path} `;
      });
    },
    [],
  );

  const handleRemoveMention = useCallback((filePath: string) => {
    setValue((prev) => prev.replace(`@workspace:${filePath} `, "").replace(`@workspace:${filePath}`, ""));
  }, []);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const newVal = e.target.value.slice(0, 4000);
      setValue(newVal);

      // Detect @ at end of input (or after space) to trigger mention picker
      if (
        workspaceId &&
        newVal.endsWith("@") &&
        (newVal.length === 1 || newVal[newVal.length - 2] === " ")
      ) {
        setMentionOpen(true);
      }

      // Detect / at the start of input to trigger command picker
      if (commandJustSelected.current) {
        // Don't reopen popover right after selecting a command
        commandJustSelected.current = false;
      } else if (newVal === "/") {
        // Only open on a fresh "/" keystroke (not when value already has /command text)
        setCommandOpen(true);
      } else if (!newVal.startsWith("/")) {
        setCommandOpen(false);
      }

      // Detect trailing `#<query>` to trigger the Drive file mention picker.
      // Uses whitespace-or-start boundary so `foo#bar` doesn't open it.
      const driveQuery = detectDriveTrigger(newVal);
      if (driveQuery !== null) {
        setDriveMentionOpen(true);
        setDriveMentionQuery(driveQuery);
      } else if (driveMentionOpen) {
        setDriveMentionOpen(false);
      }
    },
    [workspaceId, driveMentionOpen],
  );

  const handleDriveMentionSelect = useCallback(
    ({ name, url }: { name: string; url: string }) => {
      setValue((prev) => insertDriveMention(prev, `[${name}](${url})`));
      setDriveMentionOpen(false);
      setDriveMentionQuery("");
      textareaRef.current?.focus();
    },
    [],
  );

  return (
    <div
      className={cn(
        isTerminal
          ? "shrink-0 bg-[var(--card)]/60 backdrop-blur-md border-t border-[var(--chat-surface-mid)] px-10 py-6"
          : "shrink-0 border-t bg-card px-6 py-4",
      )}
    >
      <div className="mx-auto max-w-3xl">
        {/* Attachment chips */}
        {mentions.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {mentions.map((path) => (
              <span
                key={path}
                className={cn(
                  "inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium",
                  isTerminal
                    ? "rounded-sm bg-[var(--chat-surface-high)] text-[var(--chat-accent)]"
                    : "rounded-full bg-primary/10 text-primary",
                )}
              >
                {path.split("/").pop()}
                <button
                  onClick={() => handleRemoveMention(path)}
                  className="hover:text-destructive"
                  title={`Remove ${path}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        {attachedFile && (
          <div className="flex items-center gap-2 px-3 py-1.5 mb-1 bg-blue-500/10 rounded-md text-[12px]">
            <FileSpreadsheet className="h-3.5 w-3.5 text-blue-400" />
            <span className="text-foreground">{attachedFile.name}</span>
            <button onClick={() => setAttachedFile(null)} className="text-muted-foreground hover:text-foreground">
              <X className="h-3 w-3" />
            </button>
          </div>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept=".xlsx,.csv,.xls"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) handleFileUpload(file);
            if (e.target) e.target.value = "";
          }}
        />
        <div
          className={cn(
            isTerminal
              ? "relative flex items-end gap-3 p-2 bg-[var(--card)] border border-[var(--chat-surface-mid)] shadow-2xl group"
              : "relative flex items-end gap-3 rounded-2xl border bg-background p-2 shadow-soft transition-shadow focus-within:shadow-soft-md focus-within:ring-1 focus-within:ring-ring",
          )}
        >
          <textarea
            ref={textareaRef}
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={
              workspaceId
                ? "Ask a question... (type / for skills, @ for workspace files, # for Drive files)"
                : "Ask a question... (type / for skills, # for Drive files)"
            }
            disabled={isLoading}
            rows={1}
            className="flex-1 resize-none bg-transparent px-2 py-1.5 text-[14px] placeholder:text-muted-foreground focus-visible:outline-none disabled:opacity-50"
            style={{ minHeight: "2rem", maxHeight: "8rem" }}
            onInput={(e) => {
              const target = e.target as HTMLTextAreaElement;
              target.style.height = "auto";
              const newHeight = Math.min(target.scrollHeight, 128);
              target.style.height = `${newHeight}px`;
              // Notify parent to re-anchor scroll after layout shift
              requestAnimationFrame(() => {
                const msgList = document.querySelector("[data-testid='message-list']");
                if (msgList) msgList.scrollTop = msgList.scrollHeight;
              });
            }}
          />
          {workspaceId && (
            <FileMentionPicker
              open={mentionOpen}
              onOpenChange={setMentionOpen}
              workspaceId={workspaceId}
              onSelect={handleMentionSelect}
            >
              <Button
                size="icon"
                variant="ghost"
                className="h-8 w-8 shrink-0 rounded-xl"
                onClick={() => setMentionOpen(!mentionOpen)}
                aria-label="Mention file"
                title="Reference a workspace file"
              >
                <AtSign className="h-4 w-4" />
              </Button>
            </FileMentionPicker>
          )}
          <div className="relative">
            <DriveFileMentionPicker
              open={driveMentionOpen}
              query={driveMentionQuery}
              onSelect={handleDriveMentionSelect}
              onClose={() => setDriveMentionOpen(false)}
            />
          </div>
          <button
            onClick={() => fileInputRef.current?.click()}
            className={cn(
              "p-1.5 rounded transition-colors",
              isTerminal
                ? "text-[var(--chat-accent)]/60 hover:text-[var(--chat-accent)]"
                : "text-muted-foreground hover:text-foreground",
            )}
            title="Attach file"
          >
            <Paperclip className="h-4 w-4" />
          </button>
          {isRunning ? (
            <Button
              size="icon"
              variant="destructive"
              className="h-8 w-8 rounded-lg shrink-0"
              onClick={onStop}
              title="Stop response"
            >
              <Square className="h-4 w-4" />
            </Button>
          ) : isTerminal ? (
            <button
              onClick={handleSend}
              disabled={!value.trim() || isLoading}
              aria-label="Send message"
              title="Send message"
              className="w-10 h-10 flex items-center justify-center bg-[var(--chat-accent)] text-white hover:bg-[var(--chat-accent-hover)] transition-all active:scale-95 disabled:opacity-50"
            >
              <ArrowRight className="h-4 w-4" />
            </button>
          ) : (
            <Button
              size="icon"
              className="h-8 w-8 shrink-0 rounded-xl"
              onClick={handleSend}
              disabled={!value.trim() || isLoading}
              aria-label="Send message"
              title="Send message"
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          )}

          {/* Skill / Command popover */}
          {commandOpen && filteredCommands.length > 0 && (
            <div
              className={cn(
                "absolute bottom-full mb-2 left-2 z-50 w-80 p-1 shadow-lg overflow-hidden max-h-[400px] overflow-y-auto",
                isTerminal
                  ? "rounded-sm border bg-[#131313] border-[var(--chat-surface-mid)] backdrop-blur-none"
                  : "rounded-xl border bg-card",
              )}
            >
              <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground flex items-center gap-1.5">
                <Slash className="h-3 w-3" />
                Skills & Commands
              </div>
              {filteredCommands.map((cmd, idx) => (
                <button
                  key={cmd.trigger}
                  className={cn(
                    "flex w-full items-start gap-2 px-2 py-2 text-sm transition-colors",
                    isTerminal ? "rounded-none" : "rounded-lg",
                    idx === selectedIndex
                      ? isTerminal
                        ? "bg-[var(--chat-accent)] text-white"
                        : "bg-primary text-primary-foreground"
                      : "text-foreground hover:bg-muted",
                  )}
                  onClick={() => handleCommandSelect(cmd)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                >
                  <span className="font-mono text-xs shrink-0 mt-0.5">{cmd.trigger}</span>
                  <span className="text-left">
                    <span
                      className={cn(
                        "block text-[13px] font-medium",
                        idx === selectedIndex
                          ? isTerminal
                            ? "text-white"
                            : "text-primary-foreground"
                          : "text-foreground",
                      )}
                    >
                      {cmd.name}
                    </span>
                    <span
                      className={cn(
                        "block text-[11px] leading-tight",
                        idx === selectedIndex
                          ? isTerminal
                            ? "text-black/70"
                            : "text-primary-foreground/70"
                          : "text-muted-foreground",
                      )}
                    >
                      {cmd.description.length > 80 ? cmd.description.slice(0, 80) + "…" : cmd.description}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        {isTerminal && (
          <div className="h-[2px] bg-[var(--chat-surface-mid)] transition-colors duration-500 group-focus-within:bg-[var(--chat-accent)]" />
        )}
        {!isTerminal && (
          <p className="mt-1.5 text-right text-[11px] tabular-nums text-muted-foreground">
            {value.length}/4000
          </p>
        )}
        {isTerminal && (
          <div className="flex justify-between items-center mt-3">
            <span className="text-[9px] font-label text-muted-foreground uppercase tracking-widest">
              Tokens: {value.length}
            </span>
            <div className="flex gap-4">
              <button className="text-[9px] font-label text-muted-foreground hover:text-[var(--chat-accent)] uppercase tracking-widest">Clear History</button>
              <button className="text-[9px] font-label text-muted-foreground hover:text-[var(--chat-accent)] uppercase tracking-widest">Export Log</button>
            </div>
          </div>
        )}
      </div>

      <AnalyticsDashboard open={analyticsOpen} onOpenChange={setAnalyticsOpen} />
    </div>
  );
}
