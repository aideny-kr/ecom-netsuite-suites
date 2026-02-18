"use client";

import { Check, Play } from "lucide-react";
import { cn } from "@/lib/utils";
import type { OnboardingChecklistItem } from "@/lib/types";

const STEP_LABELS = [
  { key: "profile", label: "Business Profile" },
  { key: "connection", label: "NetSuite Connection" },
  { key: "policy", label: "Policy Setup" },
  { key: "workspace", label: "Workspace Setup" },
  { key: "first_success", label: "First Success" },
];

interface WizardHeaderProps {
  currentStep: number;
  checklist: OnboardingChecklistItem[];
  onVideoClick?: (stepKey: string) => void;
}

export function WizardHeader({ currentStep, checklist, onVideoClick }: WizardHeaderProps) {
  const step = STEP_LABELS[currentStep];

  return (
    <div className="border-b px-6 py-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Step {currentStep + 1} of {STEP_LABELS.length}
          </p>
          <h2 className="text-lg font-semibold mt-0.5">{step?.label}</h2>
        </div>
        {onVideoClick && (
          <button
            onClick={() => onVideoClick(step?.key || "")}
            className="flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
          >
            <Play className="h-3 w-3" />
            Watch video
          </button>
        )}
      </div>

      {/* Progress dots */}
      <div className="flex items-center gap-2 mt-4">
        {STEP_LABELS.map((s, idx) => {
          const item = checklist.find((c) => c.step_key === s.key);
          const isCompleted = item?.status === "completed" || item?.status === "skipped";
          const isCurrent = idx === currentStep;

          return (
            <div key={s.key} className="flex items-center gap-2">
              <div className="flex items-center gap-1.5">
                <div
                  className={cn(
                    "flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold transition-colors",
                    isCompleted
                      ? "bg-primary text-primary-foreground"
                      : isCurrent
                        ? "border-2 border-primary text-primary"
                        : "border border-muted-foreground/30 text-muted-foreground/50"
                  )}
                >
                  {isCompleted ? <Check className="h-3 w-3" /> : idx + 1}
                </div>
                <span
                  className={cn(
                    "text-xs font-medium hidden sm:inline",
                    isCurrent ? "text-foreground" : "text-muted-foreground/60"
                  )}
                >
                  {s.label}
                </span>
              </div>
              {idx < STEP_LABELS.length - 1 && (
                <div
                  className={cn(
                    "h-px w-4 sm:w-8",
                    isCompleted ? "bg-primary/40" : "bg-muted-foreground/15"
                  )}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
