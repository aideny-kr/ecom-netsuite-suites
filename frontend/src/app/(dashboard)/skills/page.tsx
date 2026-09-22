"use client";

import { useMemo, useState } from "react";
import { Sparkles, Search } from "lucide-react";

import { SkillCard } from "@/components/skills/skill-card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAgentSkills } from "@/hooks/use-agent-skills";

export default function SkillsPage() {
  const { data, isLoading, error } = useAgentSkills();
  const [search, setSearch] = useState("");

  // Memoize so the empty-fallback array is stable across renders (otherwise the
  // `filtered` useMemo below would recompute every render).
  const skills = useMemo(() => data ?? [], [data]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        s.triggers.some((t) => t.toLowerCase().includes(q)),
    );
  }, [skills, search]);

  return (
    <div className="animate-fade-in space-y-8">
      <div>
        <div>
          <p className="orbital-eyebrow">Expertise to reuse</p>
          <h1 className="mt-2 text-2xl font-medium text-foreground">Skills</h1>
          <p className="mt-2 text-[13px] text-muted-foreground">
            Reusable expertise for chat, with current tool requirements. Search, then
            open a command in the composer.
          </p>
        </div>
      </div>

      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search skills..."
          className="h-10 pl-9 text-[13px]"
          aria-label="Search skills"
        />
      </div>

      {isLoading ? (
        <div
          data-testid="skills-loading"
          className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
        >
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 w-full rounded-xl" />
          ))}
        </div>
      ) : error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6 text-[13px] text-destructive">
          Failed to load skills. Please try again.
        </div>
      ) : skills.length === 0 ? (
        <div className="rounded-xl border bg-card p-10 text-center shadow-soft">
          <Sparkles className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-[15px] font-medium text-foreground">
            No skills available yet
          </p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Skills appear here once they are registered for your workspace.
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl border bg-card p-10 text-center shadow-soft">
          <Sparkles className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-[15px] font-medium text-foreground">
            No skills match your search
          </p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Try a different keyword or clear the search.
          </p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((skill) => (
            <SkillCard key={skill.slug} skill={skill} />
          ))}
        </div>
      )}
    </div>
  );
}
