"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/providers/auth-provider";
import { useFeatures } from "@/hooks/use-features";
import { getWebMcpContext, registerSuiteStudioTools, type WebMcpPageState } from "@/lib/webmcp";

export function SuiteStudioWebMcp() {
  const { user, isLoading } = useAuth();
  const { data: features } = useFeatures();
  const pathname = usePathname();
  const router = useRouter();
  const state = useRef<WebMcpPageState>({ user: null, pathname, features, navigate: router.push });

  useLayoutEffect(() => {
    state.current = { user: isLoading ? null : user, pathname, features, navigate: router.push };
  }, [user, isLoading, pathname, features, router]);

  const userId = user?.id;
  const tenantId = user?.tenant_id;
  useEffect(() => {
    if (isLoading || !userId || !tenantId) return;
    const context = getWebMcpContext();
    if (!context) return;
    return registerSuiteStudioTools(context, () => state.current);
  }, [isLoading, userId, tenantId]);

  return null;
}
