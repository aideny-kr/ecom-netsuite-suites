import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// reactflow measures node/container dimensions via ResizeObserver, which jsdom
// does not implement. Stub it so the canvas can mount under vitest.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(global as any).ResizeObserver = (global as any).ResizeObserver || ResizeObserverStub;

afterEach(() => {
  cleanup();
});
