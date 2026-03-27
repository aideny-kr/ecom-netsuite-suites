"use client";

import { useState } from "react";
import { ChevronDown, ChevronUp, Edit3, Save, X } from "lucide-react";

interface InstructionPanelProps {
  agentId: string;
  instructions: string | null;
  canEdit: boolean;
  onSave: (instructions: string) => void;
  lastUpdated?: string | null;
}

export function InstructionPanel({ agentId, instructions, canEdit, onSave, lastUpdated }: InstructionPanelProps) {
  const [expanded, setExpanded] = useState(true);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(instructions || "");

  const handleSave = () => {
    onSave(draft);
    setEditing(false);
  };

  if (!instructions && !canEdit) return null;

  return (
    <div className="border-b bg-amber-50/40 dark:bg-amber-950/10">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between px-4 py-2 text-[12px] font-medium text-amber-800 dark:text-amber-200"
      >
        <span>Agent Instructions</span>
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      {expanded && (
        <div className="px-4 pb-3">
          {editing ? (
            <div>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                maxLength={5000}
                rows={4}
                className="w-full rounded-md border bg-background px-3 py-2 text-[13px] text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="Add custom instructions for this agent..."
              />
              <div className="mt-2 flex items-center justify-between">
                <span className="text-[11px] text-muted-foreground">{draft.length}/5000</span>
                <div className="flex gap-2">
                  <button onClick={() => setEditing(false)} className="text-[11px] text-muted-foreground hover:text-foreground">
                    <X className="h-3 w-3" />
                  </button>
                  <button onClick={handleSave} className="flex items-center gap-1 text-[11px] font-medium text-primary hover:text-primary/80">
                    <Save className="h-3 w-3" /> Save
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div>
              {instructions ? (
                <p className="text-[13px] leading-relaxed text-foreground/80 whitespace-pre-wrap">{instructions}</p>
              ) : (
                <p className="text-[13px] italic text-muted-foreground">No custom instructions set.</p>
              )}
              {canEdit && (
                <button
                  onClick={() => { setDraft(instructions || ""); setEditing(true); }}
                  className="mt-2 flex items-center gap-1 text-[11px] font-medium text-primary hover:text-primary/80"
                >
                  <Edit3 className="h-3 w-3" /> Edit
                </button>
              )}
              {lastUpdated && (
                <p className="mt-1 text-[10px] text-muted-foreground">Last updated: {new Date(lastUpdated).toLocaleDateString()}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
