"use client";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from "@/components/ui/tooltip";

interface ConfidenceBadgeProps {
  score: number;
  explanation?: string;
}

function getScoreStyle(score: number) {
  if (score >= 4.5)
    return { bg: "bg-emerald-100", text: "text-emerald-700", label: "Very High" };
  if (score >= 3.5)
    return { bg: "bg-sky-100", text: "text-sky-700", label: "High" };
  if (score >= 2.5)
    return { bg: "bg-yellow-100", text: "text-yellow-700", label: "Medium" };
  return { bg: "bg-orange-100", text: "text-orange-700", label: "Low" };
}

export function ConfidenceBadge({ score, explanation }: ConfidenceBadgeProps) {
  const style = getScoreStyle(score);
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${style.bg} ${style.text}`}
          >
            <span className="opacity-60">Confidence</span>
            {score.toFixed(1)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs">
          <p className="text-[13px] font-medium">
            {style.label} Confidence ({score.toFixed(1)}/5.0)
          </p>
          {explanation && (
            <p className="text-[12px] text-muted-foreground mt-1">{explanation}</p>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
