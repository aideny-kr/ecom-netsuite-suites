import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { URL as NodeURL } from "node:url";
import { presentReportHtml } from "./report-presentation";
import { reportReadingFixture } from "./__fixtures__/report-presentation";

const parse = (html: string) => new DOMParser().parseFromString(html, "text/html");
describe("frozen report reading presentation", () => {
  it.each(["inventory", "financial"])("preserves every %s figure, chart and native control", family => {
    const html = readFileSync(new NodeURL(`../../e2e/fixtures/reports/${family}.html`, import.meta.url), "utf8");
    const before = parse(html), after = parse(presentReportHtml(html));
    expect(after.querySelector(`.orbital-${family}`)).not.toBeNull();
    for (const selector of ["table", ".ia-kpis", ".fs-kpis", ".narr", ".fs-narr", "summary"]) {
      expect(Array.from(after.querySelectorAll(selector)).map(el => el.textContent)).toEqual(Array.from(before.querySelectorAll(selector)).map(el => el.textContent));
    }
    for (const selector of ["svg", "input", ".fs-good", ".fs-bad"]) {
      expect(Array.from(after.querySelectorAll(selector)).map(el => el.outerHTML)).toEqual(Array.from(before.querySelectorAll(selector)).map(el => el.outerHTML));
    }
    expect(after.querySelectorAll(".orbital-chart-scroll").length).toBeGreaterThan(0);
    for (const region of Array.from(after.querySelectorAll(".orbital-chart-scroll, .orbital-table-scroll"))) {
      expect(region.getAttribute("tabindex")).toBe("0");
      expect(region.getAttribute("aria-label")).toContain("scroll");
    }
  });
  it("exposes complete labels and exact signed values without changing the source narrative/table", () => {
    const doc = parse(presentReportHtml(reportReadingFixture));
    const rows = Array.from(doc.querySelectorAll(".orbital-drivers tbody tr"));
    expect(rows.map(r => r.querySelector("th")?.textContent)).toEqual([
      "10001 - A very long receivables category with an unabridged name", "10002 - Inventory & equipment", "10003 - Zero balance",
    ]);
    expect(rows.map(r => r.querySelector(".orbital-exact")?.textContent)).toEqual(["-1,234.567891", "2,500", "0"]);
    expect(doc.querySelector(".orbital-report > .nb-card:last-of-type")?.textContent).toContain("The complete original narrative stays here.");
    expect(doc.querySelector(".orbital-report > .nb-card:last-of-type td:last-child")?.textContent).toBe("12,345.67");
    expect(doc.querySelector(".orbital-missing")?.textContent).toBe("Value not included in this report");
    const bars = rows.map(r => r.querySelector<HTMLElement>(".orbital-bar")!);
    const negativeEnd = parseFloat(bars[0].style.left) + parseFloat(bars[0].style.width);
    expect(negativeEnd).toBeCloseTo(parseFloat(bars[1].style.left));
    expect(parseFloat(bars[2].style.width)).toBe(0);
  });
  it("leaves unknown and active documents unchanged and adds no executable content", () => {
    for (const html of ["<p>Other report</p>", reportReadingFixture.replace("</body>", "<script>alert(1)</script></body>")]) {
      expect(presentReportHtml(html)).toBe(html);
    }
    const doc = parse(presentReportHtml(reportReadingFixture));
    expect(doc.querySelector("script, iframe, object, embed")).toBeNull();
    expect(doc.querySelectorAll(".orbital-exact")).toHaveLength(3);
  });
  it("retains the source chart's category-limit disclosure with the exact values", () => {
    const disclosure = "Showing 12 largest of 50 categories";
    const input = reportReadingFixture.replace("</svg>", `<text x="692" y="44">${disclosure}</text></svg>`);
    const doc = parse(presentReportHtml(input));
    expect(doc.querySelector(".orbital-drivers")?.textContent).toContain(disclosure);
    expect(Array.from(doc.querySelectorAll(".orbital-exact")).map(el => el.textContent)).toEqual(["-1,234.567891", "2,500", "0"]);
  });
  it("preserves document metadata and supports wide built-in reports", () => {
    const input = reportReadingFixture.replace('lang="en"', 'lang="fr" dir="ltr"').replace('<body>', '<body class="tenant-report" data-note="a &gt; b">').replace('class="report"', 'class="report report--wide"');
    const doc = parse(presentReportHtml(input));
    expect(doc.documentElement.lang).toBe("fr");
    expect(doc.documentElement.dir).toBe("ltr");
    expect(doc.body.className).toBe("tenant-report");
    expect(doc.body.dataset.note).toBe("a > b");
    expect(doc.head.querySelector("title")?.textContent).toBe("Report layout fixture");
    expect(doc.querySelectorAll(".orbital-drivers tbody tr")).toHaveLength(3);
  });
  it("does not reinterpret multi-series, line charts or unparseable figures", () => {
    for (const chart of [reportReadingFixture.replace('class="ser-0"', 'class="ser-1"'), reportReadingFixture.replace("</svg>", "<path d='M0 0L1 1'/></svg>"), reportReadingFixture.replace("2,500", "unknown")]) {
      const before = parse(chart), after = parse(presentReportHtml(chart));
      expect(after.querySelector("svg")?.outerHTML).toBe(before.querySelector("svg")?.outerHTML);
      expect(after.querySelector(".orbital-drivers")).toBeNull();
    }
  });
});
