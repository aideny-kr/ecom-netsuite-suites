"use client";

import {
  Children,
  isValidElement,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";

const groups = [
  {
    id: "workspace",
    label: "Workspace",
    detail: "Profile, branding and preferences",
    keywords:
      "account name email role industry business team size logo color domain plan",
  },
  {
    id: "connections",
    label: "Connections",
    detail: "Systems and access methods",
    keywords:
      "netsuite oauth mcp api bigquery celigo shopify stripe google sheets drive metabase credentials",
  },
  {
    id: "agent",
    label: "Agent",
    detail: "Models, behavior and controls",
    keywords:
      "ai provider key model soul personality skills memory governance approvals financial limits",
  },
  {
    id: "team",
    label: "Team & access",
    detail: "People and permissions",
    keywords: "members invite invitation roles users",
  },
  {
    id: "advanced",
    label: "Advanced",
    detail: "Discovery and maintenance",
    keywords:
      "jobs schedules audit metadata scripts suitescript files sync diagnostics",
  },
];
function validSection(value: string) {
  return groups.some((g) => g.id === value);
}

export function SettingsSection({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: ReactNode;
}) {
  return (
    <section aria-label={`${label} settings`} className="min-w-0 space-y-8">
      {children}
    </section>
  );
}

export function SettingsSections({
  initialSection,
  children,
}: {
  initialSection: string;
  children: ReactNode;
}) {
  const [active, setActive] = useState(
    validSection(initialSection) ? initialSection : "workspace",
  );
  const [search, setSearch] = useState("");
  const container = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    let observer: MutationObserver | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let frame: number | undefined;
    const clear = () => {
      observer?.disconnect();
      if (timeout) clearTimeout(timeout);
      if (frame) cancelAnimationFrame(frame);
    };
    const sync = () => {
      clear();
      const hash = window.location.hash.slice(1);
      const reveal = () => {
        const target = document.getElementById(hash);
        const section = target?.closest<HTMLElement>("[data-settings-section]")?.dataset.settingsSection;
        setActive(validSection(hash) ? hash : section || (hash === "celigo" ? "connections" : initialSection));
        if (!target) return false;
        let ancestor = target.parentElement;
        while (ancestor) {
          if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
          ancestor = ancestor.parentElement;
        }
        observer?.disconnect();
        if (timeout) clearTimeout(timeout);
        if (!validSection(hash)) frame = requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
        return true;
      };
      // Feature-gated connector forms can arrive after the initial hash effect.
      // Observe only this settings tree, only for a pending legacy anchor.
      if (!reveal() && hash && !validSection(hash) && container.current) {
        observer = new MutationObserver(reveal);
        observer.observe(container.current, { childList: true, subtree: true });
        timeout = setTimeout(() => observer?.disconnect(), 30000);
      }
    };
    sync();
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      clear();
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, [initialSection]);
  const filtered = groups.filter((g) =>
    `${g.label} ${g.detail} ${g.keywords}`
      .toLowerCase()
      .includes(search.toLowerCase().trim()),
  );
  return (
    <div ref={container} className="grid min-w-0 grid-cols-[minmax(0,1fr)] items-start gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
      <div className="min-w-0 space-y-4 lg:sticky lg:top-6">
        <div className="relative">
          <Search
            aria-hidden="true"
            className="absolute left-3 top-3 h-4 w-4 text-muted-foreground"
          />
          <label htmlFor="settings-search" className="sr-only">
            Find a setting
          </label>
          <input
            id="settings-search"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Find a setting…"
            className="h-10 w-full rounded-lg border bg-card pl-9 pr-3 text-sm"
          />
        </div>
        <nav
          aria-label="Settings sections"
          className="flex gap-2 overflow-x-auto lg:flex-col"
        >
          {filtered.map((g) => (
            <a
              key={g.id}
              href={`#${g.id}`}
              aria-current={active === g.id ? "location" : undefined}
              onClick={() => setActive(g.id)}
              className={cn(
                "min-w-40 rounded-lg border p-3 text-sm transition-colors",
                active === g.id
                  ? "border-primary/65 bg-accent text-accent-foreground"
                  : "border-transparent text-muted-foreground hover:border-primary/40 hover:text-foreground hover:bg-accent",
              )}
            >
              <span className="block font-medium">{g.label}</span>
              <span className="mt-1 block text-xs text-muted-foreground">
                {g.detail}
              </span>
            </a>
          ))}
        </nav>
        {!filtered.length && (
          <p role="status" className="text-sm text-muted-foreground">
            No matching setting. Try a system or feature name.
          </p>
        )}
        <p className="text-xs leading-relaxed text-muted-foreground">
          Each form saves separately. Edits stay in place when you switch
          sections.
        </p>
      </div>
      <div className="min-w-0">
        {Children.map(children, (child) =>
          isValidElement<{ id: string }>(child) ? (
            <div
              hidden={child.props.id !== active}
              data-settings-section={child.props.id}
            >
              {child}
            </div>
          ) : (
            child
          ),
        )}
      </div>
    </div>
  );
}
