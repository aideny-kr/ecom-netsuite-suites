/**
 * The build id of THIS bundle. Inlined at build time from
 * `NEXT_PUBLIC_BUILD_ID` (set via `--build-arg` in the FE Docker build).
 *
 * The client bundle and the server `/version` route each carry the build id
 * of the build they were compiled in. When a new container is deployed, an
 * open tab still runs the OLD client BUILD_ID while `/version` (served by the
 * new container) returns the NEW one — that mismatch is how we detect skew.
 *
 * Falls back to `"dev"` locally, which disables the version check entirely.
 */
export const BUILD_ID = process.env.NEXT_PUBLIC_BUILD_ID || "dev";
