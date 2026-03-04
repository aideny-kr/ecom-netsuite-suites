"use client";

import { useState, useEffect } from "react";
import { ShieldAlert, X } from "lucide-react";

export function ImpersonationBanner() {
  const [tenantName, setTenantName] = useState<string | null>(null);

  useEffect(() => {
    setTenantName(localStorage.getItem("impersonating_tenant"));
  }, []);

  if (!tenantName) return null;

  function handleExit() {
    const adminToken = localStorage.getItem("admin_token");
    if (adminToken) {
      localStorage.setItem("access_token", adminToken);
      document.cookie = `access_token=${adminToken}; path=/; max-age=${60 * 60}; samesite=lax`;
      localStorage.removeItem("admin_token");
      localStorage.removeItem("impersonating_tenant");
      window.location.href = "/admin/dashboard";
    }
  }

  return (
    <div className="fixed top-0 left-0 right-0 z-[100] flex items-center justify-center gap-3 bg-amber-500 px-4 py-1.5 text-[13px] font-medium text-black">
      <ShieldAlert className="h-4 w-4" />
      <span>Impersonating: <strong>{tenantName}</strong></span>
      <button
        onClick={handleExit}
        className="ml-2 inline-flex items-center gap-1 rounded-md bg-black/20 px-2.5 py-0.5 text-[12px] font-semibold transition-colors hover:bg-black/30"
      >
        <X className="h-3 w-3" />
        Exit
      </button>
    </div>
  );
}
