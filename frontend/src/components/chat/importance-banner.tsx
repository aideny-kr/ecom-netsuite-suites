"use client";

import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { Shield, ShieldAlert, ShieldCheck, Info, ChevronDown } from "lucide-react";
import { useState, memo } from "react";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuLabel,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface ImportanceBannerProps {
    tier: number;
    messageId?: string;
    onOverride?: (messageId: string, newTier: number) => void;
}

const TIER_CONFIG: Record<number, {
    label: string;
    icon: typeof Shield;
    description: string;
}> = {
    1: {
        label: "Casual",
        icon: Shield,
        description: "Quick lookup — standard validation, no additional verification needed.",
    },
    2: {
        label: "Operational",
        icon: ShieldCheck,
        description: "Day-to-day business queries — results verified by AI judge with moderate confidence requirements (60%+).",
    },
    3: {
        label: "Reporting",
        icon: ShieldAlert,
        description: "Financial/management reporting — AI judge requires high confidence (80%+). Review data before including in reports.",
    },
    4: {
        label: "Audit Critical",
        icon: ShieldAlert,
        description: "Audit and compliance queries — strictest verification (90%+ confidence). Human review strongly recommended.",
    },
};

export const ImportanceBanner = memo(function ImportanceBanner({ tier, messageId, onOverride }: ImportanceBannerProps) {
    const { user } = useAuth();
    const [updating, setUpdating] = useState(false);
    const [expanded, setExpanded] = useState(false);

    const config = TIER_CONFIG[tier];
    if (!config) return null;

    const roles = user?.roles ?? (user?.role ? [user.role] : []);
    const isAdmin = roles.includes("admin") || roles.includes("owner");
    const Icon = config.icon;

    async function handleOverride(newTier: number) {
        if (!messageId || newTier === tier) return;
        setUpdating(true);
        try {
            await apiClient.patch(`/api/v1/chat/messages/${messageId}/importance`, {
                query_importance: newTier,
            });
            onOverride?.(messageId, newTier);
        } catch {
            // Silently fail
        } finally {
            setUpdating(false);
        }
    }

    return (
        <div className={cn(
            "mt-2 rounded-lg border text-[13px]",
            "border-[#00F0FF]/20 bg-[#00F0FF]/5",
            updating && "opacity-50",
        )}>
            <div className="flex items-center gap-2 px-3 py-2">
                <Icon className="h-4 w-4 shrink-0 text-[#00F0FF]" />
                <span className="text-muted-foreground">
                    This is {tier === 2 || tier === 4 ? "an" : "a"}{" "}
                    <span className="font-medium text-[#00F0FF]">{config.label}</span>
                    {" "}tier answer
                </span>

                <button
                    onClick={() => setExpanded(!expanded)}
                    className="rounded-full p-0.5 text-muted-foreground/60 hover:text-muted-foreground hover:bg-accent/50 transition-colors"
                >
                    <Info className="h-3.5 w-3.5" />
                </button>

                {isAdmin && messageId && (
                <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                        <button className="ml-auto flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-muted-foreground/60 hover:text-muted-foreground hover:bg-muted transition-colors">
                            Change
                            <ChevronDown className="h-3 w-3" />
                        </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-44">
                        <DropdownMenuLabel className="text-[11px] text-muted-foreground font-medium">
                            Override tier
                        </DropdownMenuLabel>
                        {[1, 2, 3, 4].map((t) => {
                            const tc = TIER_CONFIG[t];
                            if (!tc) return null;
                            const TIcon = tc.icon;
                            return (
                                <DropdownMenuItem
                                    key={t}
                                    onClick={() => handleOverride(t)}
                                    disabled={updating}
                                    className={cn(
                                        "gap-2 text-[12px]",
                                        t === tier && "bg-[#00F0FF]/10 font-medium"
                                    )}
                                >
                                    <TIcon className="h-3 w-3 text-[#00F0FF]" />
                                    {tc.label}
                                </DropdownMenuItem>
                            );
                        })}
                    </DropdownMenuContent>
                </DropdownMenu>
            )}
            </div>

            {expanded && (
                <div className="border-t border-[#00F0FF]/10 px-3 py-2 text-[12px] text-muted-foreground/80 space-y-1.5">
                    <div><span className="font-medium text-muted-foreground">Casual (Tier 1):</span> Quick lookup — standard validation, no additional verification needed.</div>
                    <div><span className="font-medium text-[#00F0FF]">Operational (Tier 2):</span> Day-to-day business queries — results verified by AI judge with moderate confidence (60%+).</div>
                    <div><span className="font-medium text-[#00F0FF]/80">Reporting (Tier 3):</span> Financial/management reporting — AI judge requires high confidence (80%+). Review before reports.</div>
                    <div><span className="font-medium text-[#00F0FF]/60">Audit Critical (Tier 4):</span> Audit and compliance — strictest verification (90%+). Human review strongly recommended.</div>
                </div>
            )}
        </div>
    );
});
