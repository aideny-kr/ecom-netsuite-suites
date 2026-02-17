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

export const NAV_ITEMS = [
  { label: "Dashboard", href: "/dashboard", icon: "LayoutDashboard" as const },
  { label: "Connections", href: "/connections", icon: "Plug" as const },
  { label: "Audit Log", href: "/audit", icon: "ScrollText" as const },
  { label: "Chat", href: "/chat", icon: "MessageSquare" as const },
] as const;
