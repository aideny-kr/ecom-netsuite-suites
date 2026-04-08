"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { DisclosureBlock } from "@/lib/types";
import { apiClient } from "@/lib/api-client";

interface DisclosureFooterProps {
  disclosure: DisclosureBlock;
  sessionId?: string;
  messageId?: string;
}

export function DisclosureFooter({ disclosure, sessionId, messageId }: DisclosureFooterProps) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails =
    disclosure.implicit_filters.length > 0 || disclosure.can_switch_source;
  const otherSource = disclosure.source === "netsuite" ? "BigQuery" : "NetSuite";
  const sourceLabel = disclosure.source === "netsuite" ? "NetSuite" : "BigQuery";
  const borderClass = disclosure.failure_mode
    ? "border-amber-500/20"
    : "border-border/30";

  const handleClick = () => {
    if (!hasDetails) return;
    const next = !expanded;
    setExpanded(next);
    if (next && sessionId && messageId) {
      apiClient
        .post("/api/v1/disclosure-events/expanded", {
          session_id: sessionId,
          message_id: messageId,
        })
        .catch(() => {
          /* fire-and-forget — telemetry failures must not affect UX */
        });
    }
  };

  return (
    <div
      className={`mt-3 pt-2 border-t ${borderClass} text-[11px] sm:text-[12px] italic text-muted-foreground/70`}
    >
      <button
        type="button"
        onClick={handleClick}
        className="flex items-start gap-1.5 text-left hover:text-muted-foreground transition-colors disabled:cursor-default"
        disabled={!hasDetails}
      >
        <span>
          Read from{" "}
          <span className="not-italic font-medium">{sourceLabel}</span>
          {disclosure.is_rerun && " (re-ran after source switch)"}
          {disclosure.interpretation ? `. ${disclosure.interpretation}` : "."}
        </span>
        {hasDetails && (
          <ChevronDown
            className={`h-3 w-3 mt-0.5 shrink-0 transition-transform ${
              expanded ? "rotate-180" : ""
            }`}
          />
        )}
      </button>
      {expanded && (
        <ul className="mt-1.5 ml-1 space-y-0.5">
          {disclosure.implicit_filters.map((f, i) => (
            <li key={i}>• {f}</li>
          ))}
          {disclosure.can_switch_source && !disclosure.is_rerun && (
            <li className="mt-1 text-muted-foreground/60">
              Say &ldquo;use {otherSource}&rdquo; to switch source.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
