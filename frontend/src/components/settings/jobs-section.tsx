"use client";

import { usePermissions } from "@/hooks/use-permissions";
import { useJobHistory, useJobSchedules, useTriggerJob } from "@/hooks/use-jobs";
import type { JobHistoryItem } from "@/hooks/use-jobs";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Loader2,
  Play,
  Clock,
  CheckCircle,
  XCircle,
  Bot,
  Search,
  Database,
} from "lucide-react";

const JOB_CARDS = [
  {
    name: "Knowledge Crawler",
    description:
      "Crawls NetSuite docs and blogs to expand AI knowledge. Runs daily at 3:00 AM UTC.",
    taskName: "knowledge_crawler",
    icon: Bot,
    schedule: "Daily 3:00 AM UTC",
  },
  {
    name: "Auto-Learning",
    description:
      "Detects knowledge gaps from failed queries and researches solutions. Runs daily at 4:00 AM UTC.",
    taskName: "auto_learning",
    icon: Search,
    schedule: "Daily 4:00 AM UTC",
  },
  {
    name: "Metadata Discovery",
    description:
      "Discovers custom fields, records, and org structure from NetSuite.",
    taskName: "metadata_discovery",
    icon: Database,
    schedule: "On-demand",
  },
];

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case "completed":
      return (
        <Badge variant="outline" className="border-emerald-500/30 bg-emerald-500/10 text-emerald-600">
          <CheckCircle className="mr-1 h-3 w-3" />
          Done
        </Badge>
      );
    case "running":
      return (
        <Badge variant="outline" className="border-blue-500/30 bg-blue-500/10 text-blue-600 animate-pulse">
          <Loader2 className="mr-1 h-3 w-3 animate-spin" />
          Running
        </Badge>
      );
    case "failed":
      return (
        <Badge variant="outline" className="border-red-500/30 bg-red-500/10 text-red-600">
          <XCircle className="mr-1 h-3 w-3" />
          Failed
        </Badge>
      );
    default:
      return (
        <Badge variant="outline" className="text-muted-foreground">
          {status}
        </Badge>
      );
  }
}

function formatDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt || !completedAt) return "";
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 0) return "";
  const secs = Math.floor(ms / 1000);
  const mins = Math.floor(secs / 60);
  const remainSecs = secs % 60;
  if (mins > 0) return `${mins}m ${remainSecs}s`;
  return `${secs}s`;
}

function JobCard({
  name,
  description,
  schedule,
  taskName,
  icon: Icon,
  lastRun,
}: {
  name: string;
  description: string;
  schedule: string;
  taskName: string;
  icon: typeof Bot;
  lastRun?: JobHistoryItem;
}) {
  const trigger = useTriggerJob();
  const { toast } = useToast();

  return (
    <div className="rounded-lg border p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-md bg-muted p-2">
            <Icon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div>
            <p className="text-[14px] font-semibold">{name}</p>
            <p className="text-[12px] text-muted-foreground mt-0.5">
              {description}
            </p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            trigger.mutate(taskName, {
              onSuccess: () =>
                toast({ title: `${name} triggered` }),
              onError: (err) =>
                toast({
                  title: "Failed to trigger",
                  description: err.message,
                  variant: "destructive",
                }),
            });
          }}
          disabled={trigger.isPending}
        >
          {trigger.isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <Play className="mr-1.5 h-3.5 w-3.5" />
          )}
          Run Now
        </Button>
      </div>
      <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <Clock className="h-3 w-3" />
          {schedule}
        </span>
        {lastRun && (
          <span>
            Last: {lastRun.started_at
              ? new Date(lastRun.started_at).toLocaleDateString()
              : "N/A"}{" "}
            — <StatusBadge status={lastRun.status} />
          </span>
        )}
      </div>
    </div>
  );
}

export function JobsSection() {
  const { isAdmin } = usePermissions();
  const { data: jobsData } = useJobHistory(10);
  useJobSchedules();

  if (!isAdmin) return null;

  const jobs = jobsData?.items ?? [];

  // Find last run for each task name
  const lastRunMap: Record<string, JobHistoryItem> = {};
  for (const job of jobs) {
    if (!lastRunMap[job.job_type]) {
      lastRunMap[job.job_type] = job;
    }
  }

  return (
    <section className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold tracking-tight text-foreground">
          Scheduled Jobs
        </h3>
        <p className="text-[13px] text-muted-foreground mt-0.5">
          Automated tasks that keep your AI agent up to date
        </p>
      </div>

      <div className="grid gap-3">
        {JOB_CARDS.map((card) => (
          <JobCard
            key={card.taskName}
            {...card}
            lastRun={lastRunMap[card.taskName]}
          />
        ))}
      </div>

      {jobs.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-[14px] font-medium text-foreground">
            Recent Job History
          </h4>
          <div className="rounded-lg border overflow-hidden">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="text-left py-2 px-3 font-medium text-muted-foreground">
                    Job
                  </th>
                  <th className="text-left py-2 px-3 font-medium text-muted-foreground">
                    Started
                  </th>
                  <th className="text-left py-2 px-3 font-medium text-muted-foreground">
                    Duration
                  </th>
                  <th className="text-left py-2 px-3 font-medium text-muted-foreground">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => (
                  <tr key={job.id} className="border-b last:border-b-0">
                    <td className="py-2 px-3 font-mono text-[12px]">
                      {job.job_type}
                    </td>
                    <td className="py-2 px-3 text-muted-foreground">
                      {job.started_at
                        ? new Date(job.started_at).toLocaleString(undefined, {
                            month: "short",
                            day: "numeric",
                            hour: "numeric",
                            minute: "2-digit",
                          })
                        : "—"}
                    </td>
                    <td className="py-2 px-3 text-muted-foreground">
                      {formatDuration(job.started_at, job.completed_at) || "—"}
                    </td>
                    <td className="py-2 px-3">
                      <StatusBadge status={job.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
