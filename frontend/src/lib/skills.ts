import type { AgentSkillMetadata } from "@/lib/types";

// A skill's primary slash command: its first `/`-prefixed trigger, falling back
// to the first trigger of any shape. Single source of truth shared by the chat
// composer's slash-menu (chat-input.tsx) and the Skills page (skill-card.tsx) so
// the two can never disagree on a skill's command.
export function primarySlash(skill: AgentSkillMetadata): string {
  return skill.triggers.find((t) => t.startsWith("/")) ?? skill.triggers[0];
}
