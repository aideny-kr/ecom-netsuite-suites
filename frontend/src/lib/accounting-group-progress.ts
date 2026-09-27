/**
 * Live state of each order in an approved group correction.
 *
 * The group card used to count "verified" from each order's preparation-time snapshot, which
 * never changes during a run: it read "0 verified" while the dispatch record said 18 orders
 * were written (2026-09-27). An order's receipt, once it exists, is its final word; before
 * that, the live dispatch record says where it is.
 */

export type LiveState =
  | "reconciled"
  | "further_review"
  | "rechecking"
  | "writing"
  | "queued"
  | "refused"
  | "held"
  | "needs_review";

export const LIVE_LABEL: Record<LiveState, string> = {
  reconciled: "Reconciled",
  further_review: "Verified · further review",
  rechecking: "Written · rechecking",
  writing: "Writing",
  queued: "Queued",
  refused: "Refused · nothing sent",
  held: "Held · not sent",
  needs_review: "Needs review",
};

interface GroupMember {
  case_id?: string;
  order_reference: string;
  confirmation_id?: string;
  card?: { accounting_receipt?: { status?: string } | null } | null;
  resolution_receipt?: { status?: string } | null;
}

interface Dispatch {
  members?: Record<string, { status?: string }>;
}

export function liveState(member: GroupMember, dispatch?: Dispatch | null): LiveState | null {
  if (!member.card) return null;
  const receipt = member.resolution_receipt || member.card.accounting_receipt;
  if (receipt?.status === "reconciled") return "reconciled";
  if (receipt?.status === "partially_resolved") return "further_review";
  if (receipt?.status === "needs_review") return "needs_review";
  const status = member.confirmation_id ? dispatch?.members?.[member.confirmation_id]?.status : undefined;
  switch (status) {
    case "verified":
      return "rechecking";
    case "dispatching":
      return "writing";
    case "queued":
      return "queued";
    case "rejected":
      return "refused";
    case "blocked": // held back when the group stopped: finished for this run, nothing sent
      return "held";
    case "needs_review":
    case "verification_pending":
      return "needs_review";
    default:
      return null;
  }
}

export function groupProgress(members: GroupMember[], dispatch?: Dispatch | null) {
  const counts: Record<LiveState, number> = {
    reconciled: 0, further_review: 0, rechecking: 0, writing: 0, queued: 0, refused: 0, held: 0, needs_review: 0,
  };
  const writing: string[] = [];
  let total = 0;
  for (const member of members) {
    if (!member.card) continue;
    total += 1;
    const state = liveState(member, dispatch);
    if (!state) continue;
    counts[state] += 1;
    if (state === "writing") writing.push(member.order_reference);
  }
  // Done = every order with an outcome for this run: reconciled, written and rechecking, refused,
  // held back when the group stopped, or needing review.
  const done =
    counts.reconciled + counts.further_review + counts.rechecking + counts.refused + counts.held +
    counts.needs_review;
  return { counts, writing, total, done };
}
