"use client";

import { useParams } from "next/navigation";
import { JobDetail } from "@/components/scheduled-jobs/job-detail";

export default function ScheduledJobDetailPage() {
  const { id } = useParams<{ id: string }>();
  return <JobDetail id={id} />;
}
