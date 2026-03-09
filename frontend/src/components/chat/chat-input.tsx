"use client";

import { useState, useCallback, useMemo, useEffect, useRef, type KeyboardEvent } from "react";
import { ArrowUp, AtSign, X, Slash } from "lucide-react";
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
}

export function ChatInput({ onSend, isLoading, workspaceId }: ChatInputProps) {
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
  }, [value, isLoading, onSend]);

  const handleCommandSelect = useCallback(
    (cmd: (typeof commands)[0]) => {
      if (cmd.type === "builtin" && cmd.trigger === "/export_analytics") {
        setCommandOpen(false);
        setValue("");
        setAnalyticsOpen(true);
        return;
      }

      // Agent skill — autocomplete the trigger into the input
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
      if (newVal.startsWith("/")) {
        setCommandOpen(true);
      } else {
        setCommandOpen(false);
      }
    },
    [workspaceId],
  );

  return (
    <div className="shrink-0 border-t bg-card px-6 py-4">
      <div className="mx-auto max-w-3xl">
        {/* Attachment chips */}
        {mentions.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {mentions.map((path) => (
              <span
                key={path}
                className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary"
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
        <div className="relative flex items-end gap-3 rounded-2xl border bg-background p-2 shadow-soft transition-shadow focus-within:shadow-soft-md focus-within:ring-1 focus-within:ring-ring">
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

          {/* Skill / Command popover */}
          {commandOpen && filteredCommands.length > 0 && (
            <div className="absolute bottom-full mb-2 left-2 z-50 w-80 rounded-xl border bg-card p-1 shadow-lg overflow-hidden">
              <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground flex items-center gap-1.5">
                <Slash className="h-3 w-3" />
                Skills & Commands
              </div>
              {filteredCommands.map((cmd, idx) => (
                <button
                  key={cmd.trigger}
                  className={`flex w-full items-start gap-2 rounded-lg px-2 py-2 text-sm transition-colors ${
                    idx === selectedIndex
                      ? "bg-primary text-primary-foreground"
                      : "text-foreground hover:bg-muted"
                  }`}
                  onClick={() => handleCommandSelect(cmd)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                >
                  <span className="font-mono text-xs shrink-0 mt-0.5">{cmd.trigger}</span>
                  <span className="text-left">
                    <span className={`block text-[13px] font-medium ${idx === selectedIndex ? "text-primary-foreground" : "text-foreground"}`}>
                      {cmd.name}
                    </span>
                    <span className={`block text-[11px] leading-tight ${idx === selectedIndex ? "text-primary-foreground/70" : "text-muted-foreground"}`}>
                      {cmd.description.length > 80 ? cmd.description.slice(0, 80) + "…" : cmd.description}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        <p className="mt-1.5 text-right text-[11px] tabular-nums text-muted-foreground">
          {value.length}/4000
        </p>
      </div>

      <AnalyticsDashboard open={analyticsOpen} onOpenChange={setAnalyticsOpen} />
    </div>
  );
}
