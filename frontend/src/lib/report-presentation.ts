import { REPORT_FAMILY_CSS } from "./report-family-styles";

/** Presentation of supported frozen report templates. Never persisted or used
 * for downloads. The caller must keep the resulting document in sandbox="". */
export function presentReportHtml(html: string): string {
  try {
    // Template contents stay inert: never insert report nodes into the app document.
    const template = document.createElement("template");
    template.innerHTML = html;
    const report = template.content.querySelector<HTMLElement>(".report");
    if (!report || !report.querySelector(".nb-card, .ia-section, .fs-stmt")) return html;
    // Preserve arbitrary authored/active documents byte-for-byte. This adapter only
    // understands the built-in static HTML/CSS/SVG templates, not active report pages.
    if (template.content.querySelector("script, iframe, object, embed, img, image, link, base")) return html;
    report.classList.add("orbital-report");
    if (report.querySelector(".ia-section")) report.classList.add("orbital-inventory");
    if (report.querySelector(".fs-stmt")) report.classList.add("orbital-financial");

    // Native trend SVGs keep every node/coordinate/tooltip. Give the plot its own
    // scroll region on narrow screens instead of shrinking its text to fit.
    for (const svg of Array.from(report.querySelectorAll<SVGSVGElement>(".chart > svg, .fs-scroll > svg"))) {
      const wrapper = document.createElement("div");
      wrapper.className = "orbital-chart-scroll";
      const width = Number(svg.getAttribute("viewBox")?.trim().split(/\s+/)[2]);
      wrapper.style.setProperty("--plot-width", `${Number.isFinite(width) && width > 0 ? Math.min(960, Math.max(480, width)) : 640}px`);
      wrapper.tabIndex = 0; wrapper.setAttribute("role", "region");
      wrapper.setAttribute("aria-label", "Chart — scroll horizontally for the complete view");
      svg.before(wrapper); wrapper.append(svg);
      const hint = document.createElement("p"); hint.className = "orbital-scroll-hint";
      hint.textContent = "Scroll horizontally to explore the full chart."; wrapper.before(hint);
    }
    for (const card of Array.from(report.querySelectorAll<HTMLElement>(".tblcard, .fs-scroll, .table-wrap"))) {
      if (!card.querySelector("table")) continue;
      card.classList.add("orbital-table-scroll"); card.tabIndex = 0;
      card.setAttribute("role", "region");
      const title = card.querySelector("h3")?.textContent || card.closest(".ia-section")?.querySelector("h2")?.textContent || "Report table";
      card.setAttribute("aria-label", `${title.trim()} — scroll to view all columns`);
      if ((card.querySelector("tr")?.children.length ?? 0) > 4) {
        const hint = document.createElement("p"); hint.className = "orbital-scroll-hint";
        hint.textContent = "Scroll horizontally to see all columns."; card.before(hint);
      }
    }

    for (const svg of Array.from(report.querySelectorAll<SVGSVGElement>(".svg-wrap > svg"))) {
      const groups = Array.from(svg.querySelectorAll<SVGGElement>(":scope > g"));
      const title = svg.querySelector(":scope > text")?.textContent?.trim();
      // Recognize only the server's single-series bars. Lines, stacked/multi-series
      // charts, special statements and unknown geometry retain their original SVG.
      if (svg.getAttribute("viewBox") !== "0 0 720 380" || !title || !groups.length ||
        groups.some(g => g.getAttribute("class") !== "ser-0" || g.querySelectorAll(":scope > rect").length !== 2) ||
        svg.querySelector("path, polyline, polygon, circle") || svg.parentElement?.querySelector(".chart-legend")) continue;
      const points = groups.map(g => {
        const text = g.querySelector(":scope > title")?.textContent ?? "";
        const match = /^([\s\S]*) — ([\s\S]*): (-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)$/.exec(text);
        if (!match) return null;
        const value = Number(match[3].replaceAll(",", ""));
        return Number.isFinite(value) ? { label: match[1], series: match[2], exact: match[3], value } : null;
      });
      if (points.some(p => !p) || new Set(points.map(p => p?.series)).size !== 1) continue;
      const data = points.filter((p): p is NonNullable<typeof p> => p !== null);
      const low = Math.min(0, ...data.map(p => p.value)), high = Math.max(0, ...data.map(p => p.value));
      const span = high - low || 1;
      if (!Number.isFinite(span)) continue;
      const figure = document.createElement("figure");
      figure.className = "orbital-drivers";
      const caption = document.createElement("figcaption");
      caption.textContent = title;
      figure.append(caption);
      const table = document.createElement("table");
      table.setAttribute("aria-label", title);
      const head = table.createTHead().insertRow();
      for (const label of ["Category", "Relative size", data[0].series]) {
        const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = label; head.append(cell);
      }
      const body = table.createTBody();
      for (const point of data) {
        const row = body.insertRow();
        const label = document.createElement("th"); label.scope = "row"; label.textContent = point.label; row.append(label);
        const plot = row.insertCell(); plot.className = "orbital-bar-cell"; plot.setAttribute("aria-hidden", "true");
        const track = document.createElement("span"); track.className = "orbital-bar-track";
        track.style.setProperty("--zero", `${-low / span * 100}%`);
        const bar = document.createElement("span"); bar.className = "orbital-bar";
        // Floating point is used only for drawing positions, never displayed values.
        bar.style.left = `${(Math.min(point.value, 0) - low) / span * 100}%`;
        bar.style.width = `${Math.abs(point.value) / span * 100}%`;
        track.append(bar); plot.append(track);
        const value = row.insertCell(); value.className = "orbital-exact"; value.textContent = point.exact;
      }
      figure.append(table);
      // The renderer caps category charts. Keep its completeness disclosure
      // alongside the replacement table, not inside the SVG being removed.
      for (const text of Array.from(svg.querySelectorAll(":scope > text"))) {
        if (!/^Showing \d+ largest of \d+ categories$/.test(text.textContent?.trim() ?? "")) continue;
        const disclosure = document.createElement("p");
        disclosure.className = "orbital-chart-note";
        disclosure.textContent = text.textContent;
        figure.append(disclosure);
      }
      const note = document.createElement("p"); note.className = "orbital-chart-note";
      note.textContent = "Bars share a zero baseline. Full values are shown as supplied by the report.";
      figure.append(note);
      svg.replaceWith(figure);
    }

    // Keep consecutive headline cards together without moving them across a heading
    // or changing their order, labels, values or source caveats.
    for (const metric of Array.from(report.querySelectorAll<HTMLElement>(":scope > .metric"))) {
      if (metric.parentElement !== report) continue;
      const grid = document.createElement("div"); grid.className = "orbital-metrics";
      metric.before(grid);
      let next: Element | null = metric;
      while (next?.classList.contains("metric")) {
        const following: Element | null = next.nextElementSibling;
        const value = next.querySelector(".value");
        if (value && !value.textContent?.trim()) {
          const missing = document.createElement("span"); missing.className = "orbital-missing";
          missing.textContent = "Value not included in this report"; value.append(missing);
        }
        grid.append(next); next = following;
      }
    }
    const style = document.createElement("style"); style.textContent = REPORT_READING_CSS + REPORT_FAMILY_CSS;
    template.content.append(style);
    return serializeReadingDocument(template, html);
  } catch { return html; }
}

/** Fragment parsing intentionally stays inert. Restore document-level attributes
 * (including language/direction) without ever parsing report nodes in the live DOM. */
function serializeReadingDocument(template: HTMLTemplateElement, source: string): string {
  const createRoot = (tag: "html" | "head" | "body") => {
    const element = document.createElement(tag);
    const opening = source.match(new RegExp(`<${tag}\\b(?:[^>"']|"[^"]*"|'[^']*')*>`, "i"))?.[0];
    if (opening) {
      const attributes = document.createElement("template");
      attributes.innerHTML = opening.replace(new RegExp(`^<${tag}`, "i"), "<div") + "</div>";
      for (const attr of Array.from(attributes.content.firstElementChild?.attributes ?? [])) element.setAttribute(attr.name, attr.value);
    }
    return element;
  };
  const root = createRoot("html"), head = createRoot("head"), body = createRoot("body");
  for (const node of Array.from(template.content.childNodes)) {
    if (node instanceof Element && ["STYLE", "META", "TITLE"].includes(node.tagName)) head.append(node);
    else body.append(node);
  }
  root.append(head, body);
  return `<!doctype html>${root.outerHTML}`;
}

const REPORT_READING_CSS = `
@media screen {
body:has(.orbital-report) { background:#f4f6f8; color:#233442; }
.orbital-report { max-width:1120px; padding:36px clamp(16px,4vw,48px) 64px; font-size:15px; line-height:1.7; }
.orbital-report h1 { font-size:clamp(26px,3.3vw,38px); font-weight:650; line-height:1.2; margin:22px 0 32px; overflow-wrap:anywhere; }
.orbital-report h2 { font-size:23px; font-weight:600; line-height:1.35; margin:36px 0 16px; }
.orbital-report .accent-bar { height:3px; border:0; background:#7ca3bb; margin-bottom:24px; }
.orbital-report .nb-card { border:1px solid #d6dfe6; border-radius:10px; box-shadow:none; padding:22px; margin:18px 0; min-width:0; }
.orbital-report p { overflow-wrap:anywhere; }
.orbital-report .metric .value { font-size:clamp(22px,2.5vw,28px); font-weight:650; line-height:1.3; letter-spacing:-.025em; overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
.orbital-report .metric .label { font-size:11px; font-weight:600; color:#536677; letter-spacing:.07em; }
.orbital-report .orbital-metrics { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin:18px 0; }
.orbital-report .orbital-metrics .nb-card { margin:0; }
.orbital-report .orbital-metrics:has(> :only-child) { grid-template-columns:1fr; }
.orbital-report .orbital-missing { display:block; font-size:13px; font-weight:400; letter-spacing:normal; color:#536677; }
.orbital-report th, .orbital-report td { border:0; border-bottom:1px solid #e0e6eb; padding:10px 12px; font-size:13px; }
.orbital-report thead th { background:#eaf0f4; color:#344d60; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; box-shadow:none; }
.orbital-report tbody tr:nth-child(even) { background:#f7f9fb; }
.orbital-report .foot { font-size:12px; color:#536677; }
.orbital-report .svg-wrap { max-width:100%; }
.orbital-report .svg-wrap > svg { display:block; max-width:none; }
.orbital-drivers { margin:0; }
.orbital-drivers figcaption { font-size:17px; font-weight:600; margin-bottom:18px; }
.orbital-drivers table { table-layout:fixed; }
.orbital-drivers thead th:first-child { width:36%; }
.orbital-drivers thead th:last-child { width:27%; text-align:right; }
.orbital-drivers tbody th { background:transparent; color:#233442; font-weight:450; overflow-wrap:anywhere; }
.orbital-drivers .orbital-exact { text-align:right; font-size:12px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.orbital-bar-track { position:relative; display:block; height:22px; border-radius:3px; background:#edf2f6; }
.orbital-bar-track::after { content:""; position:absolute; left:var(--zero); top:-4px; bottom:-4px; width:1px; background:#8396a5; }
.orbital-bar { position:absolute; top:4px; height:14px; border-radius:2px; background:#6b9dbb; }
.orbital-report .orbital-chart-note { font-size:11px; color:#536677; margin:18px 0 0; }
@media(min-width:1100px) { .orbital-report .orbital-metrics { grid-template-columns:repeat(4,minmax(0,1fr)); } }
@media(max-width:640px) {
  .orbital-report { padding-top:24px; } .orbital-report .nb-card { padding:16px; }
  .orbital-report .orbital-metrics { grid-template-columns:1fr; }
  .orbital-drivers thead { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); white-space:nowrap; }
  .orbital-drivers tbody { display:block; }
  .orbital-drivers tr { display:grid; grid-template-columns:minmax(0,1fr) minmax(100px,.8fr); padding:12px 0; border-bottom:1px solid #e0e6eb; }
  .orbital-drivers tbody th, .orbital-drivers td { border:0; padding:4px; }
  .orbital-drivers .orbital-bar-cell { grid-column:1 / -1; grid-row:2; }
  .orbital-drivers .orbital-exact { grid-column:2; grid-row:1; }
}
}
@media print { .orbital-drivers { margin:0; } .orbital-drivers table { width:100%; } .orbital-drivers tr { break-inside:avoid; } .orbital-chart-note { font-size:11px; } }
`;
