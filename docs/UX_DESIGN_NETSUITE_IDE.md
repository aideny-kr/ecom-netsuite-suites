# UX Design: NetSuite SuiteScript Development Experience

## The Core Insight

Your three features aren't separate — they form a single **development loop** that every developer cycles through hundreds of times per session:

```
    ┌──────────────┐
    │  UNDERSTAND   │ ← Fetch scripts, read documentation,
    │  "What is this?" │   see relationships between files
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │   MODIFY      │ ← Edit the script, change logic,
    │  "Let me try…" │   refactor code
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │   VERIFY      │ ← Run tests with real data,
    │  "Did it work?" │   watch record changes live
    └──────┬───────┘
           │
           └──────→ back to UNDERSTAND
```

The magic isn't in any single feature. It's in the **transitions** between phases. Every time a developer has to switch tabs, wait for a response, or mentally map "which file does this relate to?" — they fall out of flow state.

The goal: **zero-friction transitions** between understand, modify, and verify.

---

## Design Principle: Cognitive Distance = 0

The research on Cursor, JetBrains, Postman, and Observable all converge on one pattern: **keep related information in the same visual space.**

JetBrains puts test pass/fail icons in the editor gutter — not in a separate test panel. Cursor makes AI aware of your current file — you don't have to tell it what you're looking at. Observable auto-updates downstream cells — you don't click "Run All" after every change.

For us, this means: the developer should never have to explain context to the tool. If they're looking at `UserEventScript_customer.js`, the tool should already know what record type it operates on, which other scripts touch the same record, and what test data looks like for that record.

---

## Feature 1: Script Fetching — "The Constellation View"

### The Problem with File Trees

Every IDE shows a file tree. But SuiteScript files aren't just files — they're **deployed artifacts** with relationships. A User Event Script is attached to a record type. A Client Script is deployed to a specific form. A Scheduled Script has a deployment status and execution frequency. A Map/Reduce has governance limits.

A flat file tree hides all of this. The developer fetches 200 scripts and stares at a wall of filenames.

### The Design: Script Galaxy

Instead of (or alongside) the traditional file tree, present a **relationship-aware view** that shows scripts the way NetSuite thinks about them.

**Level 1 — Record-centric grouping (default view)**

```
Customer
  ├── beforeLoad (UserEventScript_customer.js)
  ├── beforeSubmit (CustomerValidation_UE.js)
  ├── pageInit (CustomerForm_CS.js)
  └── 2 Saved Searches referencing this record

Sales Order
  ├── beforeSubmit (SO_TaxCalc_UE.js)
  ├── afterSubmit (SO_Fulfillment_UE.js → triggers SO_Process_MR.js)
  ├── validateLine (SO_LineValidation_CS.js)
  └── Scheduled: SO_Cleanup_SS.js (daily, 2AM)

Unattached / Library
  ├── utils/formatting.js
  └── utils/tax_rates.js
```

This is possible because we already have SuiteQL access — we can query `customscript`, `customdeploy`, and the deployment's `recordtype` field to build this map.

**Level 2 — Dependency graph (on hover/expand)**

When a developer hovers over a script, show its dependencies as a subtle graph:

```
SO_TaxCalc_UE.js
  imports → utils/tax_rates.js
  triggers → SO_Process_MR.js (via afterSubmit)
  deployed to → Sales Order (beforeSubmit)
  governance → 1000 units max
  last modified → 3 days ago by admin@company.com
```

This is the JetBrains "Quick Documentation" pattern applied to NetSuite context. No click required — hover and you understand.

**Level 3 — Selective fetch (progressive disclosure)**

The fetch experience should have three tiers:

1. **Quick scan** (default): Fetch metadata only — script names, types, deployments, record associations. Takes 2-3 seconds. Populates the constellation view. No file content downloaded yet.

2. **Open to edit**: When the developer clicks a script, *then* fetch the content via RESTlet. Show a subtle loading shimmer in the editor tab (like Figma loading assets). Content appears in <1 second.

3. **Bulk sync**: "Sync All" button for developers who want everything locally. Runs in background with a progress indicator. This is the existing batch sync — it becomes the power-user option, not the default.

**Why this order matters**: Most developers don't edit 200 files. They edit 5-10 and reference 20-30. Fetching metadata first lets them navigate intelligently, then pull content on demand. It's the Observable pattern — show the shape of the data before loading the data.

---

## Feature 2: Testing with Real Data — "The Living Sandbox"

This is your killer feature. Here's why it's different from every other testing approach:

Most SuiteScript testing is one of:
- "Deploy to sandbox and pray"
- "Write mocks from memory (often wrong)"
- "Ask the admin what the data looks like"

You're offering: **tests that run against the actual shape and values of the customer's data, with PII stripped, in real time.** No other NetSuite tool does this.

### Design A: Test Runner with Record Fixtures (the solid foundation)

```
┌─────────────────────────────────────────────────────┐
│ Editor: SO_TaxCalc_UE.js                    [Run ▶] │
│─────────────────────────────────────────────────────│
│  1  /**                                             │
│  2   * @NScriptType UserEventScript                 │
│  3   */                                             │
│  4  define(['N/record'], (record) => {              │
│  5    const beforeSubmit = (context) => {            │
│  6      const so = context.newRecord;        ●green │ ← gutter: test passes
│  7      const total = so.getValue('total');   ●green │
│  8      const tax = calculateTax(total);      ●red  │ ← gutter: test fails here
│  9      so.setValue('custbody_tax', tax);            │
│ 10    };                                            │
│─────────────────────────────────────────────────────│
│ Test Results                          [Fixtures ▾]  │
│                                                     │
│  ✓ beforeSubmit — standard order ($500)    12ms     │
│  ✓ beforeSubmit — zero-value order ($0)     8ms     │
│  ✗ beforeSubmit — multi-currency (¥12000)  FAIL     │
│    │                                                │
│    │  Expected: 1200.00                             │
│    │  Received: NaN                                 │
│    │  at line 8: calculateTax(total)                │
│    │                                                │
│    │  Fixture data (from your NetSuite):            │
│    │  { currency: "JPY", total: 12000,              │
│    │    exchangerate: 0.0067 }                      │
│    │                                                │
│    │  [Fix with AI] [View Fixture] [Edit Fixture]   │
│─────────────────────────────────────────────────────│
```

**The key UX elements:**

1. **Gutter icons** (JetBrains pattern): Green/red dots next to code lines that have test coverage. The developer sees at a glance which lines are tested and which are failing — without switching to a test panel.

2. **Fixture data sourced from their NetSuite**: The test failure shows the actual record shape from their account. The developer doesn't have to guess what a Japanese sales order looks like — they can see the real fields and values (with PII masked).

3. **"Fix with AI" inline**: When a test fails, one click sends the failing test + fixture data + error to the AI chat. The chat already has context (it knows the file, the test, the data). It suggests a fix inline. This is the Cursor pattern — the AI doesn't just explain, it proposes a code change in-place.

**Fixture generation flow:**

```
Developer opens SO_TaxCalc_UE.js
     │
     ▼
System detects: deployed to Sales Order (beforeSubmit)
     │
     ▼
"Generate test fixtures?" → Yes
     │
     ▼
Backend runs SuiteQL via MockData RESTlet:
  SELECT id, total, currency, exchangerate, custbody_tax
  FROM transaction WHERE type = 'SalesOrd' LIMIT 10
     │
     ▼
Returns 10 real sales orders (PII masked)
     │
     ▼
Auto-generates test cases:
  - Standard case (most common values)
  - Edge case (zero values, nulls)
  - Variant (different currency, different subsidiary)
     │
     ▼
Fixtures saved as JSON in __tests__/fixtures/salesorder.json
Developer reviews, adjusts, runs tests
```

**This is the Storybook pattern for NetSuite**: each test case is a "story" — a known data state that the script should handle correctly. But unlike Storybook, the stories come from real data, not imagination.

### Design B: Live Record Watcher (the killer differentiator)

This is the second path you mentioned — listen to record changes via MCP SuiteQL. Here's how that could feel:

```
┌─────────────────────────────────────────────────────┐
│ Editor: SO_TaxCalc_UE.js                            │
│─────────────────────────────────────────────────────│
│  (script content)                                   │
│─────────────────────────────────────────────────────│
│ ◉ Live Watch: Sales Order              [Stop] [⟳]  │
│                                                     │
│  14:32:01  SO-10491 created by admin                │
│    beforeSubmit fired                               │
│    total: $1,250.00 → tax: $112.50 ✓               │
│    execution: 45 units, 120ms                       │
│                                                     │
│  14:32:15  SO-10491 edited by jdoe                  │
│    beforeSubmit fired                               │
│    total: $1,800.00 → tax: $162.00 ✓               │
│    execution: 42 units, 95ms                        │
│                                                     │
│  14:33:02  SO-10492 created by api_user             │
│    beforeSubmit fired ⚠ ERROR                       │
│    TypeError: Cannot read 'getValue' of null        │
│    at line 6: context.newRecord                     │
│    [View Record] [Debug] [Create Test Case]         │
│─────────────────────────────────────────────────────│
```

**How this works technically**: Poll via SuiteQL every N seconds for recent script execution logs (`SELECT * FROM scriptexecutionlog WHERE script = 'customscript_so_taxcalc' ORDER BY date DESC`), or use the system notes / audit trail to detect record changes.

**The "Create Test Case" button is the bridge between Design A and B**: When a live execution fails, one click captures that record's data as a test fixture. Now you have a regression test generated from a real production error. This is the Observable reactive pattern — data flows from live observation into your test suite.

### Which to build first?

**Design A (Test Runner with Fixtures) is the foundation.** It works offline, it's deterministic, and it directly solves the "testing with real data" problem. Build this first.

**Design B (Live Watcher) is the wow factor.** It requires an active NetSuite connection and is inherently non-deterministic. But it's what makes developers say "I've never seen anything like this." Build this second, as an enhancement to A — specifically, as a fixture generator that watches production and captures interesting cases.

---

## Feature 3: Script Documentation — "The Narrator"

### The Wrong Way: Generate a README

Most documentation tools dump a wall of JSDoc. Developers don't read it because it's divorced from the code. It's the same problem as putting tests in a separate panel — cognitive distance.

### The Right Way: Contextual Understanding

Documentation should appear **where you need it, when you need it**, at the depth you need it.

**Layer 1 — Inline annotations (always visible)**

Subtle, muted annotations that appear in the editor gutter or as ghost text:

```javascript
const beforeSubmit = (context) => {        // ← triggers on: Sales Order save
  const so = context.newRecord;            // ← type: SalesOrd (beforeSubmit mode)
  const total = so.getValue('total');      // ← field: currency, always present
  const tax = calculateTax(total);         // ← defined in: utils/tax_rates.js:14
  so.setValue('custbody_tax', tax);        // ← field: custom, type: currency
};
```

These are generated once when the file is opened and cached. They answer the most common question developers have: "what am I looking at?" The annotations are the Figma Inspect pattern — developer-friendly metadata overlaid on the source of truth.

**Layer 2 — Hover documentation (on demand)**

Hover over `context.newRecord` and see:

```
┌────────────────────────────────────────┐
│ context.newRecord                       │
│ ─────────────────────────────────────── │
│ The record being submitted.             │
│ In beforeSubmit: contains new values.   │
│ In afterSubmit: read-only.              │
│                                         │
│ Type: Sales Order                       │
│ Available fields (from your account):   │
│   total (currency)                      │
│   entity (customer ref)                 │
│   custbody_tax (currency, custom)       │
│   subsidiary (list)                     │
│   ... 47 more fields                    │
│                                         │
│ [Show all fields] [View record in NS]   │
└────────────────────────────────────────┘
```

Notice: "Available fields **from your account**." This isn't generic SuiteScript documentation — it's documentation enriched with the customer's actual schema, pulled via SuiteQL. The developer sees the real fields, not just the API spec.

**Layer 3 — Relationship narrative (AI-generated, on request)**

When the developer asks "explain this script" or clicks a "Explain" button, the AI generates a concise narrative that includes relationships:

> **SO_TaxCalc_UE.js** — Tax calculation for Sales Orders
>
> This User Event runs on beforeSubmit for Sales Order records. It reads the order total, calculates tax using the rate table in `utils/tax_rates.js`, and writes the result to `custbody_tax`.
>
> **Upstream**: Triggered when any user saves a Sales Order (form: Standard Sales Order Form).
> **Downstream**: The calculated tax value is read by `SO_Fulfillment_UE.js` in its afterSubmit to include tax in the fulfillment record.
> **Shared dependency**: Both this script and `Invoice_TaxCalc_UE.js` import `calculateTax()` from `utils/tax_rates.js`. Changes to the tax logic affect both.
>
> **Governance**: Uses ~45 units per execution (5,000 limit). No risk.

This is generated once and cached. It answers the second most common question: "how does this fit into the bigger picture?" The AI has all the context it needs — the script content, the deployment metadata, the relationship graph from Feature 1.

---

## The Unified Experience: How It All Flows Together

Here's a realistic developer session using all three features:

```
1. Developer opens the workspace
   → Constellation View loads (metadata only, 2 seconds)
   → They see all scripts grouped by record type

2. They click on "Customer" → see 4 scripts attached
   → Hover over CustomerValidation_UE.js
   → Inline card shows: beforeSubmit, validates email + phone

3. They click to open it
   → Content fetches via RESTlet (<1 second)
   → Inline annotations appear: field types, dependencies
   → Gutter icons: no tests yet (gray dots)

4. They ask the AI: "explain this script and what it relates to"
   → AI responds with relationship narrative (concise, cached)
   → Mentions it shares validation logic with LeadConversion_SS.js

5. They want to modify the email validation regex
   → They edit line 12
   → "Generate test fixtures?" prompt appears (first time only)
   → They click yes

6. Backend pulls 10 real customer records via MockData RESTlet
   → PII masked: emails become test_0@example.com
   → But the field shapes, optional fields, null patterns are real
   → Auto-generates 3 test cases: standard, missing email, unicode name

7. They click "Run Tests" (or it auto-runs on save)
   → Jest executes in <2 seconds
   → Gutter icons turn green on lines 10-15
   → Line 12 (the regex change) shows green — their edit works

8. They push the change back to NetSuite via RESTlet
   → Toast: "Pushed successfully"
   → Constellation View updates the "last modified" timestamp

Total context switches: 0. They never left the editor.
```

---

## Visual Design Language

### Color System for State

| State | Color | Where it appears |
|-------|-------|-----------------|
| Tested + passing | Soft green | Gutter dot, test result row |
| Tested + failing | Soft red | Gutter dot, test result row, inline error |
| No test coverage | Gray | Gutter dot (subtle, not alarming) |
| Live watch event | Blue pulse | Watch panel row (fades after 3s) |
| Live watch error | Amber/orange | Watch panel row (stays highlighted) |
| Fetching/loading | Shimmer | Editor area, tab label |
| AI thinking | Muted text pulse | Chat panel |

### Animation Principles

- **Gutter icon transitions**: Fade from gray → green/red over 200ms (not instant — the developer should notice)
- **Live watch new events**: Slide in from top with a brief blue highlight, then fade to normal
- **Test results**: Expand downward with content already rendered (no loading spinner for test output)
- **Fixture generation**: Progress bar in the test panel header (not a modal — developer can keep editing)

### Information Density Tiers

```
Minimal (default):    Script name + record type + pass/fail badge
Standard (hover):     + dependency list + field types + governance
Detailed (click):     + AI narrative + full field schema + execution history
```

This is the progressive disclosure pattern used by every successful developer tool. The developer controls how much information they see by how deeply they interact.

---

## What Makes This Different From Everything Else

| Existing tools | What they lack | What we provide |
|---------------|---------------|-----------------|
| NetSuite IDE (browser) | No testing, no real data, no relationships | Full test runner with production-shape fixtures |
| VS Code + SDF | Testing requires manual mock creation | Auto-generated fixtures from live SuiteQL |
| SuiteCloud Unit Testing (standalone) | Stubs only, no real data shapes | Stubs + real record schemas from their account |
| Postman (for RESTlet testing) | No SuiteScript awareness | Understands script types, deployments, governance |
| ChatGPT (for explaining scripts) | No access to the customer's NetSuite | AI explanations enriched with their actual schema |

The combination of **real data shapes** + **relationship awareness** + **inline AI** is what no one else has. Each feature alone is incremental. Together, they create an experience where the developer understands, modifies, and verifies SuiteScript without ever leaving their flow state.

---

## Implementation Priority

### Phase 1: Foundation (makes the tool usable)
- Script fetch with metadata-first loading
- Record-centric grouping (constellation view)
- Basic test runner with Oracle stubs

### Phase 2: Differentiation (makes the tool remarkable)
- Auto-fixture generation from SuiteQL
- Gutter icons for test coverage
- Contextual hover documentation with real field schemas

### Phase 3: Delight (makes developers evangelists)
- Live record watcher
- "Create Test Case" from live failures
- AI relationship narratives
- One-click "explain this and everything it touches"
