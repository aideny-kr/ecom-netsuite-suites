import { strFromU8, strToU8, unzip, zip } from "fflate";

const NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
// XML parsing/mutation is synchronous even though ZIP work uses workers.
// Large exports retain their original workbook instead of freezing the UI
// for a cosmetic pass (50k rows measured an ~878ms main-thread pause).
const MAX_PRESENTATION_XML_BYTES = 1_000_000;

function parse(bytes: Uint8Array): Document {
  const document = new DOMParser().parseFromString(strFromU8(bytes), "application/xml");
  if (document.getElementsByTagName("parsererror").length) throw new Error("Invalid workbook XML");
  return document;
}

/** Presentation only for the existing query-export template. Preserve all data,
 * formulas, number formats, relationships and other ZIP members. Unknown formats
 * or formatting failures return the original workbook so downloads still work.
 */
export async function presentExcelExport(blob: Blob): Promise<Blob> {
  try {
    const files = await new Promise<Record<string, Uint8Array>>((resolve, reject) => {
      void blob.arrayBuffer().then(buffer => {
        unzip(new Uint8Array(buffer), (error, result) => error ? reject(error) : resolve(result));
      }, reject);
    });
    if (!files["xl/styles.xml"] || !files["xl/worksheets/sheet1.xml"]) return blob;
    if (Object.keys(files).filter(path => /^xl\/worksheets\/sheet\d+\.xml$/.test(path)).length !== 1) return blob;
    if (files["xl/styles.xml"].byteLength + files["xl/worksheets/sheet1.xml"].byteLength > MAX_PRESENTATION_XML_BYTES) return blob;
    const styles = parse(files["xl/styles.xml"]);
    const sheet = parse(files["xl/worksheets/sheet1.xml"]);
    const all = (root: Document | Element, name: string) => Array.from(root.getElementsByTagNameNS(NS, name));
    const header = Number(all(sheet, "pane")[0]?.getAttribute("ySplit"));
    // Scope this adapter to our existing export layout, not arbitrary attachments.
    if (!header || !all(styles, "fgColor").some(el => el.getAttribute("rgb")?.slice(-6).toUpperCase() === "1A73E8")) return blob;

    const palette: Record<string, string> = {
      "1A73E8": "243442", "F8F9FA": "F3F6F8", "E0E0E0": "E8EDF1",
      "333333": "64748B", "666666": "64748B", "999999": "64748B",
    };
    for (const element of all(styles, "color").concat(all(styles, "fgColor"))) {
      const color = element.getAttribute("rgb")?.slice(-6).toUpperCase();
      if (color && palette[color]) element.setAttribute("rgb", `FF${palette[color]}`);
    }
    for (const font of all(styles, "font")) {
      all(font, "name")[0]?.setAttribute("val", "Arial");
      const size = all(font, "sz")[0];
      if (size) size.setAttribute("val", Number(size.getAttribute("val")) >= 14 ? "16" : "10");
    }
    // The header keeps a single fine rule. Body rows use restrained banding.
    for (const bottom of all(styles, "bottom")) {
      if (bottom.getAttribute("style") === "medium") bottom.setAttribute("style", "thin");
      else bottom.removeAttribute("style");
    }
    for (const view of all(sheet, "sheetView")) view.setAttribute("showGridLines", "0");
    for (const tab of all(sheet, "tabColor")) tab.setAttribute("rgb", "FF243442");
    all(sheet, "sheetFormatPr")[0]?.setAttribute("defaultRowHeight", "22");
    for (const row of all(sheet, "row")) {
      const index = Number(row.getAttribute("r"));
      row.setAttribute("ht", index === 1 ? "36" : index === header ? "28" : "22");
      row.setAttribute("customHeight", "1");
    }
    for (const col of all(sheet, "col")) {
      col.setAttribute("width", String(Math.max(14, Math.min(54, Number(col.getAttribute("width")) + 4))));
    }
    const title = all(sheet, "c").find(cell => cell.getAttribute("r") === "A1");
    const titleText = title && all(title, "t")[0];
    if (titleText && /^query-results(-loaded-rows)?-\d{4}-\d{2}-\d{2}$/.test(titleText.textContent ?? "")) {
      titleText.textContent = titleText.textContent?.includes("loaded-rows") ? "Query results (loaded rows)" : "Query results";
    }
    const sheetData = all(sheet, "sheetData")[0];
    // The existing template has contiguous data followed by a blank spacer and
    // its row-count footer. Include one-column results; exclude that footer.
    const dataRows: Element[] = [];
    for (const row of all(sheetData, "row")) {
      const index = Number(row.getAttribute("r"));
      if (index <= header) continue;
      if (index !== header + dataRows.length + 1) break;
      dataRows.push(row);
    }
    const headerRow = all(sheetData, "row").find(row => Number(row.getAttribute("r")) === header);
    const lastHeader = headerRow && all(headerRow, "c").at(-1)?.getAttribute("r");
    const footer = all(sheetData, "row").at(-1);
    const footerCells = footer ? all(footer, "c") : [];
    const footerIndex = header + dataRows.length + 2;
    if (footer?.getAttribute("r") === String(footerIndex) && footerCells.length === 1
      && footerCells[0].getAttribute("r") === `A${footerIndex}`
      && footerCells[0].getAttribute("t") === "inlineStr"
      && all(footerCells[0], "t")[0]?.textContent === `${dataRows.length} rows`) {
      footer.remove();
      const dimension = all(sheet, "dimension")[0];
      const ref = dimension?.getAttribute("ref");
      if (ref?.match(/\d+$/)?.[0] === String(footerIndex)) dimension.setAttribute("ref", ref.replace(/\d+$/, String(header + dataRows.length)));
    }
    if (dataRows.length && lastHeader && !all(sheet, "autoFilter").length) {
      const filter = sheet.createElementNS(NS, "autoFilter");
      filter.setAttribute("ref", `A${header}:${lastHeader.replace(/\d+$/, "")}${dataRows.at(-1)!.getAttribute("r")}`);
      sheetData.after(filter);
    }
    const serialize = (document: Document) => strToU8(new XMLSerializer().serializeToString(document));
    files["xl/styles.xml"] = serialize(styles);
    files["xl/worksheets/sheet1.xml"] = serialize(sheet);
    const bytes = await new Promise<Uint8Array>((resolve, reject) => {
      zip(files, { level: 6 }, (error, result) => error ? reject(error) : resolve(result));
    });
    return new Blob([bytes as Uint8Array<ArrayBuffer>], { type: MIME });
  } catch {
    return blob;
  }
}
