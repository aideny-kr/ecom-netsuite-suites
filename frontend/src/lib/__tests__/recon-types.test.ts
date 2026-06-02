import { describe, it, expectTypeOf } from "vitest";
import type { ReconResult, ReconBucketSummary } from "@/lib/types";

describe("recon types", () => {
  it("ReconResult has optional bucket", () => {
    expectTypeOf<ReconResult["bucket"]>().toEqualTypeOf<string | undefined>();
  });
  it("ReconBucketSummary shape", () => {
    expectTypeOf<ReconBucketSummary["matches"]["count"]>().toEqualTypeOf<number>();
  });
});
