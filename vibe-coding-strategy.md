# Vibe Coding Strategy: Model Selection & Workflow Optimization

*Research compiled March 2026 for Suite Studio AI development*

---

## The Big Picture

February 2026 was the most competitive month in AI coding history — Anthropic dropped Claude Opus 4.6 and OpenAI launched GPT-5.3 Codex on the same day, while Google's Gemini 3.1 Pro had just landed. All three models now score within 1 percentage point on SWE-Bench Verified (~80%), making raw capability nearly a tie. **The gap between the top models is smaller than the gap between either one and a bad prompt.**

This means the real leverage isn't picking "the best model" — it's picking the right model for each type of task, and more importantly, structuring your workflow so you need fewer back-and-forth cycles.

---

## Model Strengths at a Glance

### Claude Opus 4.6
- **Best at:** Complex multi-file refactoring, understanding vague intent, architecture planning, comprehensive documentation
- **SWE-bench:** 80.8% (highest)
- **Context:** 1M tokens
- **Cost:** $5/$25 per 1M tokens (input/output)
- **Superpower:** Generates exhaustive, step-by-step plans. In testing, Opus produced a 25,000-token implementation plan with all sub-streams, edge cases, and actionable steps — where Gemini gave a 2,500-token summary that was correct but not actionable.
- **Weakness:** Expensive for routine tasks. Can be overly thorough when you need quick iteration.

### Claude Sonnet 4.6
- **Best at:** Day-to-day coding, the 80% of tasks that don't need Opus-level reasoning
- **SWE-bench:** 79.6%
- **Cost:** $3/$15 per 1M tokens — 40% cheaper than Opus
- **Superpower:** Near-Opus quality at a fraction of the cost. The community consensus is Sonnet "feels the best for everyday vibe coding" and often beats more powerful models in rapid iteration because it's faster.
- **Use this as your default.** Escalate to Opus only for complex reasoning.

### Gemini 3.1 Pro
- **Best at:** Backend logic, database work, authentication, web development aesthetics
- **SWE-bench:** 80.6%
- **Cost:** $2/$12 per 1M tokens — cheapest frontier model
- **Context:** 2M tokens (largest)
- **Superpower:** Leads WebDev Arena for building functional, aesthetic web apps. Strong at backend work with minimal back-and-forth. Proactive problem-solver — when one approach fails, it autonomously tries different paths.
- **Weakness:** Tends to rewrite entire files to add a single line (wasteful). Weaker at complex UI component work. Plans are correct but lack granular actionability.

### GPT-5.3 Codex
- **Best at:** Speed, code review, terminal execution, sustained agentic loops
- **SWE-bench:** 80.0%
- **Cost:** $6/$30 per 1M tokens, but uses 2-4x fewer tokens per task
- **Superpower:** Terminal-Bench leader (77.3%). Codex-Spark runs at 1,000 tok/s. Built for cloud sandboxes and long-running autonomous tasks.
- **Weakness:** Feels like you need to babysit it more with detailed descriptions for mundane tasks. Most limited feature set compared to Claude Code.

---

## The Verdict: Mix Models, Don't Pick One

**The answer is definitively: mix them.** The emerging best practice in 2026 is "model routing" — selecting the optimal model per task type. Teams using this approach report 70-80% cost reduction while maintaining quality. Think of it like delegating to the right team member.

### Recommended Router for Suite Studio AI Development

| Task Type | Model | Why |
|-----------|-------|-----|
| **Architecture & system design** | Claude Opus 4.6 | Exhaustive plans, considers edge cases, multi-file reasoning |
| **Daily coding & iteration** | Claude Sonnet 4.6 | 90% of Opus quality, fastest iteration loop |
| **Complex debugging** | Claude Opus 4.6 | Better at holding full context and reasoning through chains |
| **Backend API / DB logic** | Gemini 3.1 Pro | Minimal back-and-forth for straightforward backend work, cheapest |
| **UI/frontend components** | Claude Sonnet 4.6 | Stronger at complex React/component architecture |
| **Code review** | GPT-5.3 Codex | Fast, good at spotting issues, uses fewer tokens |
| **SuiteScript / NetSuite work** | Claude Opus 4.6 | Better at domain-specific reasoning with large context |
| **Quick fixes & simple changes** | Claude Sonnet 4.6 or Gemini 3.1 Pro | Don't waste Opus on trivial tasks |
| **Research & exploration** | Gemini 3.1 Pro | 2M context window, cheapest for processing large amounts of info |
| **Long-running autonomous tasks** | GPT-5.3 Codex | Built for sustained agentic loops in sandboxes |

### The 90/10 Rule
Most production teams implement this: **90% of requests go to Sonnet 4.6, 10% escalate to Opus 4.6.** The escalation triggers are:
- Multi-file refactoring (3+ files changing together)
- Greenfield architecture decisions
- Debugging that requires understanding 5+ interacting components
- Vague or ambiguous requirements that need interpretation

---

## Why You're Prompting "So Many Little Things"

This is the most common frustration in vibe coding, and it's usually caused by one of these patterns:

### Problem 1: Jumping Straight to Code
The AI starts writing code before understanding what you actually want. Then you course-correct, it rewrites, you course-correct again.

**Fix: Plan Before Code.**
Tell the model explicitly: *"Do not write any code. First, create a detailed plan for how you would implement this. Wait for my approval before writing anything."* This single change reduced back-and-forth by 68% in Microsoft's research.

### Problem 2: Not Enough Upfront Context
The AI makes assumptions because you didn't specify constraints. Then you fix those assumptions one by one.

**Fix: Use the CTCO Framework.**
Every prompt should include:
- **Context:** What exists now, what files are involved, what patterns to follow
- **Task:** What you want done, specifically
- **Constraints:** What NOT to do, what patterns to preserve, what to be careful about
- **Output:** What the deliverable looks like

### Problem 3: No Persistent Memory
Each session starts fresh. The AI doesn't remember your project conventions, your NetSuite setup, or what you tried yesterday.

**Fix: Invest in CLAUDE.md (or equivalent).**
Your CLAUDE.md is actually quite good already. The key additions for reducing friction:
- Add common SuiteQL patterns the agent should know
- Add your specific NetSuite customizations and field names
- Add "don't do X, do Y instead" rules from past frustrations
- Keep a "current state" section updated after each session

### Problem 4: Too Big, Too Vague
Asking "build the entire SuiteScript sync feature" is a recipe for frustration. The model tries to do everything at once and gets details wrong.

**Fix: Decompose Into Focused Loops.**
Break every feature into steps of 50-100 lines max. Each step: plan → approve → implement → verify. The smaller the unit of work, the less rework.

---

## Practical Workflow for Suite Studio AI

Based on the research, here's the workflow optimized for your project:

### Phase 1: Architecture (Opus 4.6)
Use Opus to plan features end-to-end. Give it your CLAUDE.md, describe the feature, and ask for:
- Which files need to change
- The order of implementation
- Edge cases and error handling
- A step-by-step implementation plan

Don't let it write code yet. Just get the plan right.

### Phase 2: Implementation (Sonnet 4.6, one step at a time)
Take the Opus plan and feed each step to Sonnet. For each step:
1. Provide the specific file(s) to change
2. Reference the relevant CLAUDE.md patterns
3. Ask for the implementation
4. Review, adjust, move to next step

### Phase 3: Backend Heavy-Lifting (Gemini 3.1 Pro, optional)
For pure backend work (new API endpoints, database queries, service layers) where the pattern is well-established, Gemini can be faster and cheaper. Especially good for:
- Writing SuiteQL queries from specifications
- Creating CRUD endpoints that follow existing patterns
- Database migration files

### Phase 4: Review & Polish (Codex or Opus)
Use Codex for fast code review, or Opus for architectural review of the entire feature.

---

## Tool Selection: Claude Code vs Cursor vs Codex

You're using Claude Code (via Cowork), which is the terminal-first approach. Here's how the tools compare:

- **Claude Code:** Best for concurrent background work, sub-agents, and automation. Uses 5.5x fewer tokens than Cursor for identical tasks. Great for your workflow since you're building a complex backend.
- **Cursor:** Best if you prefer visual IDE workflow with inline suggestions and visual diffs. Better for frontend iteration where you want to see changes instantly.
- **Codex (cloud):** Best for fire-and-forget autonomous tasks. Good for "run my test suite and fix whatever breaks."

For Suite Studio AI, Claude Code (what you have) is the right fit given the backend-heavy, multi-service architecture.

---

## Key Takeaways

1. **Default to Sonnet 4.6** for everyday work. Escalate to Opus for architecture and complex reasoning.
2. **Always plan before coding.** Use Opus to create the plan, Sonnet to execute it.
3. **Invest heavily in CLAUDE.md** — every hour spent on it saves 10 hours of re-prompting.
4. **Break work into 50-100 line chunks.** Never ask for an entire feature in one prompt.
5. **Use the CTCO framework** (Context, Task, Constraints, Output) for every prompt.
6. **Gemini 3.1 Pro is the value play** for straightforward backend work and research tasks.
7. **The models are closer than ever** — your prompting strategy matters more than model selection.
