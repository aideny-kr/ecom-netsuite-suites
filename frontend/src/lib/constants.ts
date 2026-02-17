export const CANONICAL_TABLES = [
  {
    name: "orders",
    label: "Orders",
    description: "E-commerce orders from connected platforms",
  },
  {
    name: "payments",
    label: "Payments",
    description: "Payment transactions linked to orders",
  },
  {
    name: "refunds",
    label: "Refunds",
    description: "Refund records for returned or disputed orders",
  },
  {
    name: "payouts",
    label: "Payouts",
    description: "Payout batches from payment processors",
  },
  {
    name: "payout_lines",
    label: "Payout Lines",
    description: "Individual line items within payouts",
  },
  {
    name: "disputes",
    label: "Disputes",
    description: "Chargebacks and disputes from payment processors",
  },
  {
    name: "netsuite_postings",
    label: "NetSuite Postings",
    description: "Records posted or pending posting to NetSuite",
  },
] as const;

export const AI_PROVIDERS = [
  { value: "", label: "Platform Default" },
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "gemini", label: "Google Gemini" },
] as const;

export const AI_MODELS: Record<string, { value: string; label: string }[]> = {
  anthropic: [
    { value: "claude-opus-4-6", label: "Claude Opus 4.6" },
    { value: "claude-sonnet-4-5-20250929", label: "Claude Sonnet 4.5" },
    { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5" },
    { value: "claude-opus-4-5-20251101", label: "Claude Opus 4.5" },
    { value: "claude-sonnet-4-20250514", label: "Claude Sonnet 4" },
    { value: "claude-opus-4-20250514", label: "Claude Opus 4" },
  ],
  openai: [
    { value: "gpt-5.2", label: "GPT-5.2" },
    { value: "gpt-5.2-pro", label: "GPT-5.2 Pro" },
    { value: "gpt-5", label: "GPT-5" },
    { value: "gpt-5-mini", label: "GPT-5 Mini" },
    { value: "gpt-5-nano", label: "GPT-5 Nano" },
    { value: "gpt-4.1", label: "GPT-4.1" },
    { value: "gpt-4.1-mini", label: "GPT-4.1 Mini" },
    { value: "gpt-4.1-nano", label: "GPT-4.1 Nano" },
    { value: "o3", label: "o3" },
    { value: "o3-mini", label: "o3 Mini" },
    { value: "o3-pro", label: "o3 Pro" },
    { value: "o4-mini", label: "o4 Mini" },
  ],
  gemini: [
    { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
    { value: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite" },
    { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
    { value: "gemini-2.0-flash", label: "Gemini 2.0 Flash" },
    { value: "gemini-3-pro-preview", label: "Gemini 3 Pro (Preview)" },
    { value: "gemini-3-flash-preview", label: "Gemini 3 Flash (Preview)" },
  ],
};

export const PLAN_TIERS: Record<
  string,
  { label: string; color: string; bg: string }
> = {
  free: { label: "Free", color: "text-gray-600", bg: "bg-gray-100" },
  pro: { label: "Pro", color: "text-blue-600", bg: "bg-blue-100" },
  max: { label: "Max", color: "text-purple-600", bg: "bg-purple-100" },
};

export const NAV_ITEMS = [
  { label: "Dashboard", href: "/dashboard", icon: "LayoutDashboard" as const },
  { label: "Connections", href: "/connections", icon: "Plug" as const },
  { label: "Audit Log", href: "/audit", icon: "ScrollText" as const },
  { label: "Chat", href: "/chat", icon: "MessageSquare" as const },
  { label: "Dev Workspace", href: "/workspace", icon: "Code" as const },
  { label: "Settings", href: "/settings", icon: "Settings" as const },
] as const;
