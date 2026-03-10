"use client";

import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { Shield, ShieldAlert, ShieldCheck, ChevronDown } from "lucide-react";
import { useState } from "react";
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from "@/components/ui/tooltip";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuLabel,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface ImportanceBadgeProps {
    tier?: number;
    needsReview?: boolean;
    messageId?: string;
    onOverride?: (messageId: string, newTier: number) => void;
    className?: string;
}

const TIER_CONFIG: Record<number, {
    label: string;
    color: string;
    bgColor: string;
    icon: typeof Shield;
    description: string;
}> = {
    1: {
        label: "Casual",
        color: "text-muted-foreground",
        bgColor: "bg-muted/50",
        icon: Shield,
        description: "Quick lookup — standard validation",
    },
    2: {
        label: "Operational",
        color: "text-sky-600 dark:text-sky-400",
        bgColor: "bg-sky-50 dark:bg-sky-950/30",
        icon: ShieldCheck,
        description: "Operational query — verified by AI judge",
    },
    3: {
        label: "Reporting",
        color: "text-amber-600 dark:text-amber-400",
        bgColor: "bg-amber-50 dark:bg-amber-950/30",
        icon: ShieldAlert,
        description: "Reporting grade — high-confidence judge verification",
    },
    4: {
        label: "Audit Critical",
        color: "text-rose-600 dark:text-rose-400",
        bgColor: "bg-rose-50 dark:bg-rose-950/30",
        icon: ShieldAlert,
        description: "Audit critical — strictest verification applied",
    },
};

export function ImportanceBadge({ tier, needsReview, messageId, onOverride, className }: ImportanceBadgeProps) {
    const { user } = useAuth();
    const [updating, setUpdating] = useState(false);

    if (!tier || !TIER_CONFIG[tier]) return null;

    const roles = user?.roles ?? (user?.role ? [user.role] : []);
    const isAdmin = roles.includes("admin") || roles.includes("owner");
    const config = TIER_CONFIG[tier];
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
            // Silently fail — badge stays at current tier
        } finally {
            setUpdating(false);
        }
    }

    const badge = (
        <span
            className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                config.bgColor,
                config.color,
                isAdmin && messageId && "cursor-pointer hover:opacity-80",
                updating && "opacity-50",
                className
            )}
        >
            <Icon className="h-3 w-3" />
            {config.label}
            {needsReview && (
                <span className="ml-0.5 text-rose-500 dark:text-rose-400">
                    • Needs Review
                </span>
            )}
            {isAdmin && messageId && <ChevronDown className="h-2.5 w-2.5 opacity-60" />}
        </span>
    );

    // Admin with messageId: show dropdown for override
    if (isAdmin && messageId) {
        return (
            <DropdownMenu>
                <DropdownMenuTrigger asChild>{badge}</DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-44">
                    <DropdownMenuLabel className="text-[11px] text-muted-foreground font-medium">
                        Override tier
                    </DropdownMenuLabel>
                    {[1, 2, 3, 4].map((t) => {
                        const tc = TIER_CONFIG[t];
                        const TIcon = tc.icon;
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
                                <TIcon className={cn("h-3 w-3", tc.color)} />
                                {tc.label}
                            </DropdownMenuItem>
                        );
                    })}
                </DropdownMenuContent>
            </DropdownMenu>
        );
    }

    // Non-admin or no messageId: tooltip only
    return (
        <TooltipProvider>
            <Tooltip>
                <TooltipTrigger asChild>{badge}</TooltipTrigger>
                <TooltipContent side="bottom" className="text-[12px] max-w-[200px]">
                    {config.description}
                    {needsReview && (
                        <p className="mt-1 text-rose-500 dark:text-rose-400 font-medium">
                            Human verification recommended before using in official reports.
                        </p>
                    )}
                </TooltipContent>
            </Tooltip>
        </TooltipProvider>
    );
}
