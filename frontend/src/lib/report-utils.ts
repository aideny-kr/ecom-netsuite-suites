/** Creator-or-admin gate for destructive report actions (delete/pin) — mirrors the
 * backend's `_can_manage` in `backend/app/api/v1/reports.py`. */
export function canManageReport(
  user: { id: string; roles?: string[] } | null | undefined,
  createdBy: string | null | undefined,
): boolean {
  if (!user?.id) return false;
  if (createdBy && createdBy === user.id) return true;
  return Boolean(user.roles?.includes("admin"));
}

export function fmtStamp(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
