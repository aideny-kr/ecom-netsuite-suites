"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, CalendarClock, Code2, Pause, Play, Sparkles } from "lucide-react";
import { useFeatures } from "@/hooks/use-features";
import { useWorkspaces } from "@/hooks/use-workspace";
import { MetalOrbits } from "@/components/orbital/metal-orbits";
import { Button } from "@/components/ui/button";

/** Workbench's entry points live beside Chat's existing composer, not a second input. */
export function ChatWelcome() {
  const { data: features } = useFeatures();
  const { data: workspaces, isLoading, isError } = useWorkspaces();
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => { setReduced(media.matches); setHidden(document.hidden); };
    try { setPaused(localStorage.getItem("orbital-motion-paused") === "true"); } catch { /* Optional storage. */ }
    sync();
    media.addEventListener("change", sync);
    document.addEventListener("visibilitychange", sync);
    return () => {
      media.removeEventListener("change", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);

  const actions = [
    { title: "Build a workflow", href: "/scheduled-jobs/new", icon: CalendarClock, enabled: true },
    { title: "Explore skills", href: "/skills", icon: Sparkles, enabled: features?.chat !== false },
    { title: "Developer workspace", href: "/workspace", icon: Code2, enabled: features?.workspace !== false },
  ];

  return (
    <div className="h-full overflow-y-auto p-4 pt-14 scrollbar-thin md:p-6" aria-label="Start working">
      <section className="orbital-hero rounded-lg border p-4 md:p-7" data-motion-paused={paused || hidden || reduced}>
        <MetalOrbits className="chat-welcome-orbits" />
        <div className="relative z-10">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="orbital-eyebrow hidden sm:block">From question to evidence</p>
            <Button variant="ghost" size="sm" disabled={reduced} aria-pressed={paused || reduced} onClick={() => {
              const next = !paused;
              setPaused(next);
              try { localStorage.setItem("orbital-motion-paused", String(next)); } catch { /* Keep session preference. */ }
            }}>
              {paused || reduced ? <Play className="mr-2 h-3.5 w-3.5" /> : <Pause className="mr-2 h-3.5 w-3.5" />}
              {reduced ? "Reduced motion" : paused ? "Resume motion" : "Pause motion"}
            </Button>
          </div>
          <h2 className="mt-3 max-w-sm text-2xl font-medium leading-tight md:mt-6 md:text-3xl">What would you like to work on?</h2>
          <p className="mt-2 max-w-sm text-[13px] leading-relaxed text-muted-foreground md:mt-3 md:text-sm">Ask a question below, attach context, or pick a tool to get started.</p>
        </div>
      </section>
      <nav aria-label="Chat starting actions" className="mt-4 grid gap-3 sm:grid-cols-3">
        {actions.filter(action => action.enabled).map(({ title, href, icon: Icon }) => (
          <Link key={href} href={href} className="orbital-entry flex items-center gap-3 rounded-lg border bg-background p-4 text-[13px] font-medium">
            <Icon className="h-4 w-4 shrink-0 text-primary" />
            <span>{title}</span><ArrowUpRight className="ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </Link>
        ))}
      </nav>
      {features?.workspace !== false && (
        <details className="mt-4 rounded-lg border bg-background">
          <summary className="cursor-pointer rounded-lg p-4 text-sm font-medium hover:bg-accent">Your projects</summary>
          <div className="border-t px-4 pb-4 text-[13px]">
            {isLoading ? <p role="status" className="pt-3 text-muted-foreground">Loading projects…</p>
              : isError ? <p role="status" className="pt-3 text-muted-foreground">Projects could not be loaded. Open Developer workspace to try again.</p>
              : workspaces?.length ? <ul className="divide-y">{workspaces.slice(0, 4).map(workspace => (
                <li key={workspace.id} className="flex items-center justify-between gap-3 py-3"><span>{workspace.name}</span><span className="text-xs text-muted-foreground">{workspace.status}</span></li>
              ))}</ul> : <p className="pt-3 text-muted-foreground">Create or import a project in Developer workspace.</p>}
            <Link href="/workspace" className="mt-3 inline-block text-primary underline underline-offset-4">View workspaces →</Link>
          </div>
        </details>
      )}
      <nav aria-label="Workspace context" className="mt-4 flex flex-wrap gap-x-5 gap-y-3 text-xs text-muted-foreground">
        <Link href="/settings#connections" className="hover:text-primary">Review connected systems →</Link>
        <Link href="/reports" className="hover:text-primary">Open saved reports →</Link>
      </nav>
    </div>
  );
}
