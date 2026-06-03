import { describe, it, expect } from "vitest";
import { GET } from "@/app/version/route";
import { BUILD_ID } from "@/lib/build-id";

describe("GET /version", () => {
  it("returns the current BUILD_ID as JSON with no-store caching", async () => {
    const res = GET();
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const body = await res.json();
    expect(body).toEqual({ buildId: BUILD_ID });
  });
});
