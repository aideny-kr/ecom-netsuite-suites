"use client";

import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { Shield, ShieldAlert, ShieldCheck, Info, ChevronDown } from "lucide-react";
import { useState } from "react";
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
    color: string;
    borderColor: string;
    bgColor: string;
    icon: typeof Shield;
    description: string;
}> = {
    2: {
        label: "Operational",
        color: "text-sky-600 dark:text-sky-400",
        borderColor: "border-sky-200 dark:border-sky-800/50",
        bgColor: "bg-sky-50/50 dark:bg-sky-950/20",
        icon: ShieldCheck,
        description: "This query was classified as operational — results were verified by an AI judge with moderate confidence requirements.",
    },
    3: {
        label: "Reporting",
        color: "text-amber-600 dark:text-amber-400",
        borderColor: "border-amber-200 dark:border-amber-800/50",
        bgColor: "bg-amber-50/50 dark:bg-amber-950/20",
        icon: ShieldAlert,
        description: "This query was classified as reporting-grade — results were verified by an AI judge with high confidence requirements. Review before including in reports.",
    },
    4: {
        label: "Audit Critical",
        color: "text-rose-600 dark:text-rose-400",
        borderColor: "border-rose-200 dark:border-rose-800/50",
        bgColor: "bg-rose-50/50 dark:bg-rose-950/20",
        icon: ShieldAlert,
        description: "This query was classified as audit-critical — results were verified by an AI judge with the strictest confidence threshold. Human verification is strongly recommended before use in audits or compliance.",
    },
};

export function ImportanceBanner({ tier, messageId, onOverride }: ImportanceBannerProps) {
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
            config.borderColor,
            config.bgColor,
            updating && "opacity-50",
        )}>
            <div className="flex items-center gap-2 px-3 py-2">
                <Icon className={cn("h-4 w-4 shrink-0", config.color)} />
                <span className="text-muted-foreground">
                    This is {tier === 2 || tier === 4 ? "an" : "a"}{" "}
                    <span className={cn("font-medium", config.color)}>{config.label}</span>
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
                        <button className="ml-auto flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-muted-foreground/60 hover:text-muted-foreground hover:bg-accent transition-colors">
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
                            const label = tc?.label ?? "Casual";
                            const TIcon = tc?.icon ?? Shield;
                            const tColor = tc?.color ?? "text-muted-foreground";
                            return (
                                <DropdownMenuItem
                                    key={t}
                                    onClick={() => handleOverride(t)}
                                    disabled={updating}
                                    className={cn(
                                        "gap-2 text-[12px]",
                                        t === tier && "bg-accent font-medium"
                                    )}
                                >
                                    <TIcon className={cn("h-3 w-3", tColor)} />
                                    {label}
                                </DropdownMenuItem>
                            );
                        })}
                    </DropdownMenuContent>
                </DropdownMenu>
            )}
            </div>

            {expanded && (
                <div className="border-t px-3 py-2 text-[12px] text-muted-foreground/80 space-y-1.5" style={{ borderColor: "inherit" }}>
                    <div><span className="font-medium text-muted-foreground">Casual (Tier 1):</span> Quick lookup — standard validation, no additional verification needed.</div>
                    <div><span className="font-medium text-sky-600 dark:text-sky-400">Operational (Tier 2):</span> Day-to-day business queries — results verified by AI judge with moderate confidence requirements (60%+).</div>
                    <div><span className="font-medium text-amber-600 dark:text-amber-400">Reporting (Tier 3):</span> Financial/management reporting — AI judge requires high confidence (80%+). Review data before including in reports.</div>
                    <div><span className="font-medium text-rose-600 dark:text-rose-400">Audit Critical (Tier 4):</span> Audit and compliance queries — strictest verification (90%+ confidence). Human review strongly recommended.</div>
                </div>
            )}
        </div>
    );
}
