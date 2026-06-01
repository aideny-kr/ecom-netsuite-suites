// @vitest-environment node
/**
 * TDD guard for the post-build CSP injector (rich-pipe slice 1 — CSP/hydration fix).
 *
 * The packaged static export ships its strict CSP via a <meta http-equiv> tag.
 * Next's App Router emits ~5 INLINE bootstrap <script> tags (the RSC Flight
 * payload) whose bytes embed the volatile buildId + content-hashed chunk names,
 * so their sha256 hashes CANNOT be hardcoded — they must be recomputed from the
 * built bytes and injected into `script-src` post-build.
 *
 * These tests pin the one sharp edge: Chromium hashes the RAW inline body bytes
 * (the text between `>` and `</script>`), with no trim / unescape / DOM
 * re-serialization. If the injector ever drifts from byte-exact hashing, the
 * packaged CSP silently re-blocks hydration — this test is the regression wall.
 */
import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";
import { injectCspIntoHtml, extractInlineScriptHashes } from "./inject-csp.mjs";

// A base policy shaped exactly like PACKAGED_CSP but with a bare `script-src
// 'self'` that the injector must augment (never weaken) with the computed hashes.
const BASE_CSP =
  "default-src 'self'; " +
  "script-src 'self'; " +
  "style-src 'self' 'unsafe-inline'; " +
  "object-src 'none'; " +
  "base-uri 'none'; " +
  "frame-ancestors 'none';";

function sha256b64(body) {
  return createHash("sha256").update(body, "utf8").digest("base64");
}

// The injector writes the policy into a meta `content="…"` HTML-escaped (exactly
// as Next/React serialize it — `&#x27;` for `'`). Chromium reads the UNESCAPED
// policy after HTML parsing, so assertions run against the unescaped form.
function unescapeMeta(value) {
  return value
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, ">")
    .replace(/&lt;/g, "<")
    .replace(/&amp;/g, "&");
}

// Pull the (unescaped) CSP string out of the meta tag in `html`.
function metaPolicy(html) {
  const m = html.match(
    /http-equiv="Content-Security-Policy"\s+content="([^"]*)"/,
  );
  return m ? unescapeMeta(m[1]) : null;
}

function scriptSrcOf(policy) {
  return policy
    .split(";")
    .map((d) => d.trim())
    .find((d) => d.startsWith("script-src"));
}

// A fixture mirroring the real export's structure: external (src) scripts must
// be IGNORED; inline `self.__next_f.push(...)` bodies must be hashed verbatim.
const INLINE_A = '(self.__next_f=self.__next_f||[]).push([0]);self.__next_f.push([2,null])';
const INLINE_B = 'self.__next_f.push([1,"1:HL[\\"./_next/static/css/x.css\\",\\"style\\"]\\n"])';
const FIXTURE_HTML = [
  "<!DOCTYPE html><html><head>",
  '<meta http-equiv="Content-Security-Policy" content="' + BASE_CSP + '"/>',
  "</head><body>",
  '<script src="./_next/static/chunks/webpack-abc.js" async=""></script>',
  "<script>" + INLINE_A + "</script>",
  "<script>" + INLINE_B + "</script>",
  "</body></html>",
].join("");

describe("extractInlineScriptHashes", () => {
  it("hashes ONLY inline scripts, byte-exact on the raw body, ignoring src= scripts", () => {
    const hashes = extractInlineScriptHashes(FIXTURE_HTML);
    expect(hashes).toEqual([
      "sha256-" + sha256b64(INLINE_A),
      "sha256-" + sha256b64(INLINE_B),
    ]);
  });

  it("dedupes identical inline bodies into one hash", () => {
    const dupHtml =
      "<script>" + INLINE_A + "</script><script>" + INLINE_A + "</script>";
    const hashes = extractInlineScriptHashes(dupHtml);
    expect(hashes).toEqual(["sha256-" + sha256b64(INLINE_A)]);
  });

  it("does not trim / unescape — leading+trailing whitespace changes the hash", () => {
    const spaced = "<script>  " + INLINE_A + "  </script>";
    const hashes = extractInlineScriptHashes(spaced);
    // Must hash the padded bytes, NOT the trimmed body.
    expect(hashes).toEqual(["sha256-" + sha256b64("  " + INLINE_A + "  ")]);
    expect(hashes).not.toContain("sha256-" + sha256b64(INLINE_A));
  });
});

describe("injectCspIntoHtml", () => {
  it("appends every inline-script hash to script-src and rewrites the meta CSP", () => {
    const out = injectCspIntoHtml(FIXTURE_HTML, BASE_CSP);
    const policy = metaPolicy(out);
    expect(policy).not.toBeNull();
    const scriptSrc = scriptSrcOf(policy);
    expect(scriptSrc).toContain("'self'");
    expect(scriptSrc).toContain("'sha256-" + sha256b64(INLINE_A) + "'");
    expect(scriptSrc).toContain("'sha256-" + sha256b64(INLINE_B) + "'");
  });

  it("NEVER weakens script-src with unsafe-inline or unsafe-eval", () => {
    const out = injectCspIntoHtml(FIXTURE_HTML, BASE_CSP);
    const scriptSrc = scriptSrcOf(metaPolicy(out));
    expect(scriptSrc).not.toContain("'unsafe-inline'");
    expect(scriptSrc).not.toContain("'unsafe-eval'");
  });

  it("preserves non-script directives (style-src keeps unsafe-inline)", () => {
    const out = injectCspIntoHtml(FIXTURE_HTML, BASE_CSP);
    const policy = metaPolicy(out);
    expect(policy).toContain("style-src 'self' 'unsafe-inline'");
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("frame-ancestors 'none'");
  });

  it("is idempotent — re-injecting does not duplicate hashes", () => {
    const once = injectCspIntoHtml(FIXTURE_HTML, BASE_CSP);
    const twice = injectCspIntoHtml(once, BASE_CSP);
    // Byte-for-byte stable across re-injection (the strongest idempotence claim).
    expect(twice).toBe(once);
    const scriptSrc = scriptSrcOf(metaPolicy(twice));
    // Base64 contains +/ (regex metachars) — escape before counting occurrences.
    const needle = ("sha256-" + sha256b64(INLINE_A)).replace(
      /[.*+?^${}()|[\]\\]/g,
      "\\$&",
    );
    const occurrences = (scriptSrc.match(new RegExp(needle, "g")) || []).length;
    expect(occurrences).toBe(1);
  });

  it("throws if the HTML has no CSP meta tag (fail loud, never silently no-op)", () => {
    expect(() => injectCspIntoHtml("<html><head></head></html>", BASE_CSP)).toThrow();
  });
});
