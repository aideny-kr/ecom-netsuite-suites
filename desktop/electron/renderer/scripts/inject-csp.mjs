/**
 * Post-build CSP injector for the desktop Next.js static export.
 *
 * WHY THIS EXISTS
 * ---------------
 * The packaged renderer ships its strict Content-Security-Policy via a
 * `<meta http-equiv="Content-Security-Policy">` tag (Electron loads the export
 * over file://; there are no session response headers to attach a CSP to). The
 * policy keeps `script-src` strict — never `'unsafe-inline'` / `'unsafe-eval'`.
 *
 * But Next 14's App Router emits ~5 INLINE bootstrap `<script>` tags per page:
 * the serialized RSC Flight payload (`self.__next_f.push(...)`) that hydration
 * requires. Under a bare `script-src 'self'` Chromium refuses to execute them,
 * so the static HTML renders but never hydrates — the composer stays inert.
 *
 * Those inline bodies embed VOLATILE content (the random per-build `buildId`
 * and content-hashed chunk filenames), so their sha256 hashes can't be pinned
 * in source. This step recomputes them from the freshly built bytes after
 * `next build` and injects `'sha256-…'` entries into `script-src` — the only
 * way to allow exactly those scripts without weakening the policy.
 *
 * BYTE-EXACT HASHING IS THE SHARP EDGE
 * ------------------------------------
 * Chromium hashes the RAW inline body bytes — the exact UTF-8 between `>` and
 * `</script>`. We must NOT trim, HTML-unescape, or re-serialize through a DOM
 * library (any of which changes bytes and breaks the match). We slice the raw
 * string and hash it verbatim.
 *
 * Run as `node scripts/inject-csp.mjs` (chained after `next build`). It walks
 * every `*.html` under `out/` (index.html, 404.html, …) — each page has its own
 * inline scripts and therefore its own hash set.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SCRIPT_TAG_RE = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;

/**
 * Compute the `'sha256-…'` token list for every INLINE `<script>` in `html`.
 * External (`src=`) and empty-body scripts are ignored. Bodies are hashed
 * byte-exact (no trim / unescape). Order-preserving + deduped.
 *
 * @param {string} html
 * @returns {string[]} e.g. ["sha256-Q+8t…", "sha256-riq2…"]
 */
export function extractInlineScriptHashes(html) {
  const seen = new Set();
  const hashes = [];
  for (const match of html.matchAll(SCRIPT_TAG_RE)) {
    const attrs = match[1] ?? "";
    const body = match[2] ?? "";
    if (/\bsrc\s*=/.test(attrs)) continue; // external script — allowed by 'self'
    if (body.length === 0) continue; // nothing to hash
    const digest = createHash("sha256").update(body, "utf8").digest("base64");
    const token = `sha256-${digest}`;
    if (seen.has(token)) continue;
    seen.add(token);
    hashes.push(token);
  }
  return hashes;
}

/**
 * Parse a CSP string into ordered [directive, valueTokens[]] pairs.
 * @param {string} policy
 */
function parsePolicy(policy) {
  return policy
    .split(";")
    .map((d) => d.trim())
    .filter(Boolean)
    .map((d) => {
      const parts = d.split(/\s+/);
      return [parts[0], parts.slice(1)];
    });
}

/**
 * Serialize ordered directive pairs back into a CSP string (trailing `;`,
 * matching the source-of-truth PACKAGED_CSP formatting).
 */
function serializePolicy(pairs) {
  return (
    pairs
      .map(([name, values]) => [name, ...values].join(" "))
      .join("; ") + ";"
  );
}

/**
 * Build the hash-augmented policy from a canonical base policy: append every
 * inline-script hash to `script-src`. Idempotent — any pre-existing
 * `'sha256-…'` tokens in the base's script-src are dropped before re-adding, so
 * re-running yields identical output. `'self'` is preserved; no `'unsafe-*'`
 * for scripts is ever added.
 *
 * @param {string} basePolicy canonical CSP (e.g. PACKAGED_CSP)
 * @param {string[]} hashes `'sha256-…'` tokens (without surrounding quotes)
 */
export function buildAugmentedPolicy(basePolicy, hashes) {
  const pairs = parsePolicy(basePolicy);
  const scriptSrc = pairs.find(([name]) => name === "script-src");
  if (!scriptSrc) {
    throw new Error("base policy has no script-src directive");
  }
  // Drop any prior sha256 tokens (idempotence), keep everything else verbatim.
  const kept = scriptSrc[1].filter((tok) => !/^'sha256-/.test(tok));
  scriptSrc[1] = [...kept, ...hashes.map((h) => `'${h}'`)];
  return serializePolicy(pairs);
}

const HTML_META_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#x27;",
};

/** HTML-escape a CSP string for safe insertion into a meta `content="…"`. */
function escapeForMeta(value) {
  return value.replace(/[&<>"']/g, (c) => HTML_META_ESCAPES[c]);
}

const CSP_META_RE =
  /(<meta\s+http-equiv="Content-Security-Policy"\s+content=")([^"]*)("\s*\/?>)/i;

/**
 * Rewrite the CSP `<meta>` in `html` so its `script-src` allows exactly the
 * page's inline scripts (by sha256), starting from `basePolicy` as the single
 * source of truth. Throws if no CSP meta tag is present (fail loud — a silent
 * no-op would re-introduce the hydration blocker undetected).
 *
 * @param {string} html
 * @param {string} basePolicy canonical CSP (e.g. PACKAGED_CSP)
 * @returns {string} html with the meta CSP replaced
 */
export function injectCspIntoHtml(html, basePolicy) {
  if (!CSP_META_RE.test(html)) {
    throw new Error("no Content-Security-Policy <meta> tag found in HTML");
  }
  const hashes = extractInlineScriptHashes(html);
  const augmented = buildAugmentedPolicy(basePolicy, hashes);
  const escaped = escapeForMeta(augmented);
  return html.replace(CSP_META_RE, (_m, open, _old, close) => open + escaped + close);
}

/** Recursively collect every `*.html` file under `dir`. */
function findHtmlFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...findHtmlFiles(full));
    } else if (entry.endsWith(".html")) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Read PACKAGED_CSP from the source-of-truth csp.ts without importing the TS
 * module (this script is plain ESM and runs under bare node). Extracts the
 * exported template-concatenation literal.
 */
function readPackagedCspFromSource(cspTsPath) {
  const src = readFileSync(cspTsPath, "utf8");
  const m = src.match(/export\s+const\s+PACKAGED_CSP\s*=\s*([\s\S]*?);/);
  if (!m) {
    throw new Error(`could not locate PACKAGED_CSP in ${cspTsPath}`);
  }
  // The value is a series of "..." string literals concatenated with `+`.
  // Pull each double-quoted segment and join — robust to the formatting.
  const segments = [...m[1].matchAll(/"([^"]*)"/g)].map((x) => x[1]);
  if (segments.length === 0) {
    throw new Error(`PACKAGED_CSP in ${cspTsPath} is not a string-literal concat`);
  }
  return segments.join("");
}

function main() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const rendererRoot = path.resolve(here, "..");
  const outDir = path.join(rendererRoot, "out");
  const cspTsPath = path.join(rendererRoot, "src", "lib", "csp.ts");

  const basePolicy = readPackagedCspFromSource(cspTsPath);
  const htmlFiles = findHtmlFiles(outDir);
  if (htmlFiles.length === 0) {
    throw new Error(`no *.html files found under ${outDir} — did next build run?`);
  }

  let total = 0;
  for (const file of htmlFiles) {
    const before = readFileSync(file, "utf8");
    const after = injectCspIntoHtml(before, basePolicy);
    writeFileSync(file, after, "utf8");
    const count = extractInlineScriptHashes(before).length;
    total += count;
    console.log(
      `[inject-csp] ${path.relative(rendererRoot, file)} — injected ${count} inline-script hash(es)`,
    );
  }
  console.log(
    `[inject-csp] done: ${htmlFiles.length} file(s), ${total} hash injection(s) total.`,
  );
}

// Only run the CLI when invoked directly (not when imported by the test).
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
