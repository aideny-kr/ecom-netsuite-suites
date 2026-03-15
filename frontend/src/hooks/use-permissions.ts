"use client";

import { useMemo, useCallback } from "react";
import { useAuth } from "@/providers/auth-provider";
import type { RoleName } from "@/lib/types";

const ROLE_PERMISSIONS: Record<RoleName, string[]> = {
  admin: [
    "tenant.manage", "users.manage", "connections.manage", "connections.view",
    "tables.view", "audit.view", "exports.csv", "exports.excel",
    "recon.run", "tools.suiteql", "schedules.manage", "approvals.manage",
    "chat.financial_reports",
  ],
  finance: [
    "connections.view", "tables.view", "audit.view", "exports.csv",
    "exports.excel", "recon.run", "tools.suiteql", "chat.financial_reports",
  ],
  ops: [
    "connections.manage", "connections.view", "tables.view", "audit.view",
    "exports.csv", "schedules.manage",
  ],
  readonly: ["connections.view", "tables.view", "audit.view"],
};

export function usePermissions() {
  const { user } = useAuth();

  const userRoles = user?.roles;

  const permissions = useMemo(() => {
    if (!userRoles?.length) return new Set<string>();
    const perms = new Set<string>();
    for (const role of userRoles) {
      const rolePerms = ROLE_PERMISSIONS[role as RoleName];
      if (rolePerms) {
        for (const p of rolePerms) perms.add(p);
      }
    }
    return perms;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userRoles]);

  const hasPermission = useCallback(
    (codename: string) => permissions.has(codename),
    [permissions],
  );

  const isAdmin = useMemo(
    () => userRoles?.includes("admin") ?? false,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [userRoles],
  );

  return { hasPermission, isAdmin, permissions };
}
