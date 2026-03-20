"use client";

import { useState, useCallback, useMemo, useEffect, useRef, type KeyboardEvent } from "react";
import { ArrowUp, ArrowRight, AtSign, X, Slash } from "lucide-react";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { FileMentionPicker } from "@/components/chat/file-mention-picker";
import { AnalyticsDashboard } from "@/components/analytics/AnalyticsDashboard";
import { apiClient } from "@/lib/api-client";
import type { AgentSkillMetadata } from "@/lib/types";

interface ChatInputProps {
  onSend: (content: string) => void;
  isLoading: boolean;
  workspaceId?: string | null;
  variant?: "default" | "terminal";
}

export function ChatInput({ onSend, isLoading, workspaceId, variant }: ChatInputProps) {
  const isTerminal = variant === "terminal";
  const [value, setValue] = useState("");
  const [mentionOpen, setMentionOpen] = useState(false);
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

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isLoading) return;
    onSend(trimmed);
    setValue("");
    setCommandOpen(false);
  }, [value, isLoading, onSend]);

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
    },
    [workspaceId],
  );

  return (
    <div
      className={cn(
        isTerminal
          ? "shrink-0 bg-zinc-950/40 backdrop-blur-md border-t border-zinc-900/50 px-10 py-6"
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
                    ? "rounded-sm bg-zinc-800 text-[var(--chat-accent)]"
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
        <div
          className={cn(
            isTerminal
              ? "relative flex items-end gap-3 p-2 bg-[var(--chat-surface-low)] shadow-2xl group"
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
                ? "Ask a question... (type / for skills, @ for files)"
                : "Ask a question... (type / for skills)"
            }
            disabled={isLoading}
            rows={1}
            className="flex-1 resize-none bg-transparent px-2 py-1.5 text-[14px] placeholder:text-muted-foreground focus-visible:outline-none disabled:opacity-50"
            style={{ minHeight: "2rem", maxHeight: "8rem" }}
            onInput={(e) => {
              const target = e.target as HTMLTextAreaElement;
              target.style.height = "auto";
              target.style.height = `${Math.min(target.scrollHeight, 128)}px`;
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
          {isTerminal ? (
            <button
              onClick={handleSend}
              disabled={!value.trim() || isLoading}
              aria-label="Send message"
              title="Send message"
              className="w-10 h-10 flex items-center justify-center bg-[var(--chat-accent)] text-black hover:bg-[var(--chat-accent-hover)] transition-all active:scale-95 disabled:opacity-50"
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
                "absolute bottom-full mb-2 left-2 z-50 w-80 p-1 shadow-lg overflow-hidden",
                isTerminal
                  ? "rounded-none border bg-zinc-900 border-zinc-800"
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
                        ? "bg-[var(--chat-accent)] text-black"
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
                            ? "text-black"
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
          <div className="h-[2px] bg-zinc-800 transition-colors duration-500 group-focus-within:bg-[var(--chat-accent)]" />
        )}
        {!isTerminal && (
          <p className="mt-1.5 text-right text-[11px] tabular-nums text-muted-foreground">
            {value.length}/4000
          </p>
        )}
        {isTerminal && (
          <div className="flex justify-between items-center mt-3">
            <span className="text-[9px] font-label text-zinc-600 uppercase tracking-widest">
              Tokens: {value.length}
            </span>
            <div className="flex gap-4">
              <button className="text-[9px] font-label text-zinc-500 hover:text-[var(--chat-accent)] uppercase tracking-widest">Clear History</button>
              <button className="text-[9px] font-label text-zinc-500 hover:text-[var(--chat-accent)] uppercase tracking-widest">Export Log</button>
            </div>
          </div>
        )}
      </div>

      <AnalyticsDashboard open={analyticsOpen} onOpenChange={setAnalyticsOpen} />
    </div>
  );
}
