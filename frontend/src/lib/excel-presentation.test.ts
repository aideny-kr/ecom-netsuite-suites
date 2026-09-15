import { describe, it, expect } from "vitest";
import { readFile } from "node:fs/promises";
import { Blob as NodeBlob } from "node:buffer";
import { strFromU8, unzipSync, zipSync, strToU8 } from "fflate";
import { presentExcelExport } from "./excel-presentation";

const ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const xml = (data: Uint8Array) => new DOMParser().parseFromString(strFromU8(data), "application/xml");
const blob = (data: Uint8Array) => new NodeBlob([data as Uint8Array<ArrayBuffer>]) as unknown as Blob;
const bytes = (value: Blob) => new Promise<Uint8Array>(resolve => {
  const reader = new FileReader();
  reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
  reader.readAsArrayBuffer(value);
});

describe("Excel export presentation", () => {
  it("restyles the real export template without changing cells, types, number formats or other workbook parts", async () => {
    const input = await readFile("e2e/fixtures/export-loaded-rows.xlsx");
    const before = unzipSync(input);
    const output = await presentExcelExport(blob(input));
    const after = unzipSync(await bytes(output));
    const sheetBefore = xml(before["xl/worksheets/sheet1.xml"]);
    const sheetAfter = xml(after["xl/worksheets/sheet1.xml"]);
    const cells = (doc: Document) => Array.from(doc.getElementsByTagNameNS(ns,"c")).map(c=>c.outerHTML);
    expect(cells(sheetAfter)).toEqual(cells(sheetBefore).filter(cell => !cell.includes('r="A8"')));
    expect(sheetAfter.getElementsByTagNameNS(ns,"dimension")[0].getAttribute("ref")).toBe("A1:B6");
    const formats = (data: Uint8Array) => Array.from(xml(data).getElementsByTagNameNS(ns,"xf")).map(e=>e.getAttribute("numFmtId"));
    expect(formats(after["xl/styles.xml"])).toEqual(formats(before["xl/styles.xml"]));
    for (const path of Object.keys(before).filter(p=>!['xl/styles.xml','xl/worksheets/sheet1.xml'].includes(p))) expect(after[path]).toEqual(before[path]);
    expect(sheetAfter.getElementsByTagNameNS(ns,"sheetView")[0].getAttribute("showGridLines")).toBe("0");
    expect(sheetAfter.getElementsByTagNameNS(ns,"autoFilter")[0].getAttribute("ref")).toBe("A4:B6");
    expect(strFromU8(after["xl/styles.xml"])).toContain("FF243442");
    expect(sheetAfter.getElementsByTagNameNS(ns,"pane")[0].outerHTML).toBe(sheetBefore.getElementsByTagNameNS(ns,"pane")[0].outerHTML);
  });

  it("leaves unreadable and unknown workbooks downloadable", async () => {
    const invalid = blob(new Uint8Array([1,2,3]));
    expect(await presentExcelExport(invalid)).toBe(invalid);
    const unknown = blob(zipSync({"hello.txt":strToU8("unchanged")}));
    expect(await presentExcelExport(unknown)).toBe(unknown);
  });
});
