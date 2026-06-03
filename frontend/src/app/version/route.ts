import { NextResponse } from "next/server";
import { BUILD_ID } from "@/lib/build-id";

// Never cache: this route must report the build id of the running container,
// so an open tab can compare it against its own (older) inlined BUILD_ID.
export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(
    { buildId: BUILD_ID },
    { headers: { "Cache-Control": "no-store" } },
  );
}
