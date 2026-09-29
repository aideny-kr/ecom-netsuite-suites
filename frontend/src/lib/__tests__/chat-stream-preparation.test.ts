import { expect, it } from "vitest";
import { normalizeStreamEvent } from "../chat-stream";

it("keeps group preparation progress as its own stream event", () => {
  const data = { checked: 26, total: 34, ready: 20, set_aside: [{ label: "period is locked", count: 1 }], now: ["R1"] };
  expect(normalizeStreamEvent({ type: "preparation_progress", data })).toEqual({ type: "preparation_progress", data });
  expect(normalizeStreamEvent({ type: "preparation_progress", data: "nope" })).toBeNull();
});

it("drops a progress event the block could not render", () => {
  // The block reads both lists; a malformed event must not take down the message list.
  const base = { checked: 1, total: 2, ready: 1, set_aside: [], now: [] };
  expect(normalizeStreamEvent({ type: "preparation_progress", data: { ...base, set_aside: undefined } })).toBeNull();
  expect(normalizeStreamEvent({ type: "preparation_progress", data: { ...base, now: "R1" } })).toBeNull();
});
