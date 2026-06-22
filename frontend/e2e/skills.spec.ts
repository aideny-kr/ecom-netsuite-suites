import { test, expect, type Page } from "@playwright/test";

// Fully-mocked e2e (mirrors chat-rendering.spec.ts): seeded auth + page.route
// interception, so it runs deterministically without a real backend stack.
const API = "http://localhost:8000";
const APP = "http://localhost:3002";
const TOKEN =
  "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQ3NDA4NDQ4MDB9.signature";
const NOW = "2026-06-21T12:00:00.000Z";

const SKILLS = [
  {
    name: "Flux Analysis",
    description: "Period-over-period variance commentary on the P&L.",
    triggers: ["/flux"],
    slug: "flux",
  },
  {
    name: "AR Aging",
    description: "Receivables aging buckets by customer.",
    triggers: ["/aging"],
    slug: "aging",
  },
  {
    name: "Margin Bridge",
    description: "Gross margin walk between two periods.",
    triggers: ["/margin-bridge"],
    slug: "margin-bridge",
  },
];

async function seedAuth(page: Page) {
  await page.context().addCookies([
    { name: "access_token", value: TOKEN, url: APP },
  ]);
  await page.addInitScript((token: string) => {
    localStorage.setItem("access_token", token);
    // Skip the onboarding wizard so the dashboard shell renders directly.
    localStorage.setItem("onboarding_skipped", "true");
  }, TOKEN);
}

/**
 * Intercept every backend call the dashboard shell + skills + chat pages make.
 * `opts.sessions` seeds the chat session list (so we can prove "Use in chat"
 * starts a NEW chat instead of resurrecting an existing one); `opts.sessionDetail`
 * maps a session id to its detail payload (messages). Returns a probe for whether
 * a chat message was POSTed (the "did it send?" check).
 */
async function setupSkillsMocks(
  page: Page,
  opts: { sessions?: unknown[]; sessionDetail?: Record<string, unknown> } = {},
) {
  const state = { messagePosted: false };
  const sessions = opts.sessions ?? [];
  const sessionDetail = opts.sessionDetail ?? {};

  await page.route(`${API}/**`, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === "/api/v1/auth/me") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u1",
          tenant_id: "t1",
          tenant_name: "Test Tenant",
          email: "user@test.local",
          full_name: "Test User",
          role: "admin",
          onboarding_completed_at: NOW,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === "/api/v1/auth/me/tenants") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }

    if (path === "/api/v1/settings/features") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ flags: { chat: true } }),
      });
    }

    if (path === "/api/v1/onboarding/checklist/connection/validate") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ valid: true }),
      });
    }

    if (path === "/api/v1/skills/catalog") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SKILLS),
      });
    }

    // Probe: a sent message hits POST .../messages. The compose flow must NOT.
    if (path.endsWith("/messages") && method === "POST") {
      state.messagePosted = true;
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    }

    if (path === "/api/v1/chat/sessions" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessions),
      });
    }

    const sessMatch = path.match(/^\/api\/v1\/chat\/sessions\/([^/]+)$/);
    if (sessMatch && method === "GET") {
      const detail = sessionDetail[sessMatch[1]] ?? { messages: [] };
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail),
      });
    }

    // Everything else (workspaces, agents, branding, …)
    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });

  return state;
}

test.describe("Skills page", () => {
  test("golden path: browse → search → use in chat (populates, does not send)", async ({
    page,
  }) => {
    await seedAuth(page);
    const probe = await setupSkillsMocks(page);

    await page.goto("/skills");

    // Nav registration: the sidebar shows the Skills link.
    await expect(
      page.getByRole("link", { name: "Skills", exact: true }),
    ).toBeVisible();

    // The catalog renders one card per skill.
    await expect(page.getByText("Flux Analysis")).toBeVisible();
    await expect(page.getByText("AR Aging")).toBeVisible();
    await expect(page.getByText("Margin Bridge")).toBeVisible();

    // Search narrows the grid to the matching skill only.
    await page.getByLabel("Search skills").fill("flux");
    await expect(page.getByText("Flux Analysis")).toBeVisible();
    await expect(page.getByText("AR Aging")).toHaveCount(0);
    await expect(page.getByText("Margin Bridge")).toHaveCount(0);

    // "Use in chat" navigates to /chat with the compose param.
    await page.getByRole("button", { name: /use in chat/i }).click();
    await page.waitForURL("**/chat?compose=%2Fflux%20**");
    expect(page.url()).toContain("compose=%2Fflux%20");

    // The composer is populated with the slash command (+ trailing space).
    const composer = page.getByPlaceholder(/Ask a question/i);
    await expect(composer).toHaveValue("/flux ");

    // …and NOTHING was sent: the compose path only seeds the composer, it never
    // POSTs a message. Settle briefly so any (erroneous) auto-send effect would
    // have fired, then assert the message endpoint was never hit.
    await page.waitForTimeout(800);
    expect(probe.messagePosted).toBe(false);
  });

  test("'Use in chat' starts a NEW chat, not the most recent existing session", async ({
    page,
  }) => {
    await seedAuth(page);
    // Seed an existing conversation so the chat page would otherwise auto-select
    // it. The compose deep link carries new_session=true and must start fresh.
    const probe = await setupSkillsMocks(page, {
      sessions: [
        {
          id: "old-1",
          title: "Old conversation",
          workspace_id: null,
          session_type: "chat",
          is_archived: false,
          status: "completed",
          created_at: NOW,
          updated_at: NOW,
        },
      ],
      sessionDetail: {
        "old-1": {
          id: "old-1",
          title: "Old conversation",
          is_archived: false,
          created_at: NOW,
          updated_at: NOW,
          messages: [
            {
              id: "m1",
              role: "user",
              content: "OLD CONVERSATION MESSAGE",
              tool_calls: null,
              citations: null,
              created_at: NOW,
            },
          ],
        },
      },
    });

    await page.goto("/skills");
    await page.getByRole("button", { name: /use in chat/i }).first().click();
    await page.waitForURL("**/chat?compose=%2Fflux%20**");

    // The composer is seeded…
    await expect(page.getByPlaceholder(/Ask a question/i)).toHaveValue("/flux ");
    // …into a FRESH chat: the existing conversation's message must NOT appear,
    // and nothing was auto-sent.
    await page.waitForTimeout(800);
    await expect(page.getByText("OLD CONVERSATION MESSAGE")).toHaveCount(0);
    expect(probe.messagePosted).toBe(false);
  });

  test("empty state shows when no skill matches the search", async ({ page }) => {
    await seedAuth(page);
    await setupSkillsMocks(page);

    await page.goto("/skills");
    await expect(page.getByText("Flux Analysis")).toBeVisible();

    await page.getByLabel("Search skills").fill("zzzznotaskill");
    await expect(page.getByText(/no skills match/i)).toBeVisible();
    await expect(page.getByText("Flux Analysis")).toHaveCount(0);
  });
});
