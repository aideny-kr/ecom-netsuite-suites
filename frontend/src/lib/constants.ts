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
    { value: "claude-sonnet-4-20250514", label: "Claude Sonnet 4" },
    { value: "claude-haiku-4-20250414", label: "Claude Haiku 4" },
    { value: "claude-opus-4-20250514", label: "Claude Opus 4" },
  ],
  openai: [
    { value: "gpt-4o", label: "GPT-4o" },
    { value: "gpt-4o-mini", label: "GPT-4o Mini" },
    { value: "gpt-4-turbo", label: "GPT-4 Turbo" },
    { value: "o1", label: "o1" },
    { value: "o1-mini", label: "o1 Mini" },
    { value: "o3-mini", label: "o3 Mini" },
  ],
  gemini: [
    { value: "gemini-2.0-flash", label: "Gemini 2.0 Flash" },
    { value: "gemini-2.0-pro", label: "Gemini 2.0 Pro" },
    { value: "gemini-1.5-flash", label: "Gemini 1.5 Flash" },
    { value: "gemini-1.5-pro", label: "Gemini 1.5 Pro" },
  ],
};

export const NAV_ITEMS = [
  { label: "Dashboard", href: "/dashboard", icon: "LayoutDashboard" as const },
  { label: "Connections", href: "/connections", icon: "Plug" as const },
  { label: "Audit Log", href: "/audit", icon: "ScrollText" as const },
  { label: "Chat", href: "/chat", icon: "MessageSquare" as const },
  { label: "Settings", href: "/settings", icon: "Settings" as const },
] as const;
