import { redirect } from "next/navigation";

/** Preserve bookmarked Workbench links, including staged chat drafts. */
export default function WorkbenchPage({ searchParams }: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    if (Array.isArray(value)) value.forEach((item) => params.append(key, item));
    else if (value !== undefined) params.set(key, value);
  }
  redirect(`/chat${params.size ? `?${params}` : ""}`);
}
