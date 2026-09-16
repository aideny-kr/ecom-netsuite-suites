import { actionTool, boundedPreview, integerArgument } from "@/lib/webmcp-values";

interface TableState {
  name: string; page: number; pageSize: number; search: string;
  sortBy?: string; sortOrder: "asc" | "desc";
  ready: boolean; hasError: boolean; pages: number; total: number;
  columns: string[]; rows: Record<string, unknown>[];
  apply: (query: { page: number; pageSize: number; search: string; sortBy?: string; sortOrder: "asc" | "desc" }) => void;
  selectRow: (row: Record<string, unknown>) => void;
}

export function createTableTools(getState: () => TableState) {
  return [
    actionTool("table_get_state", "Read this table's visible query, readiness, columns and bounded rows. Rows are omitted while fetching so previous-page placeholders are never reported as current results.", true, {}, [], () => {
      const s = getState();
      return { table: s.name, ready: s.ready, has_error: s.hasError, page: s.page, page_size: s.pageSize,
        search: s.search, sort_by: s.sortBy, sort_order: s.sortOrder,
        total: s.ready ? s.total : null, pages: s.ready ? s.pages : null,
        columns: s.ready ? s.columns : [], ...boundedPreview(s.ready ? s.rows : []) };
    }),
    actionTool("table_set_query", "Set visible table search, sort and pagination through the UI state. Search is the supported filter. Returns requested state; poll table_get_state for matching query and ready=true before inspecting rows.", false,
      { search: { type: "string", maxLength: 200 }, page: { type: "integer", minimum: 1 }, page_size: { type: "integer", enum: [10, 25, 50, 100] },
        sort_by: { type: "string", description: "A current column; empty string clears sorting." }, sort_order: { type: "string", enum: ["asc", "desc"] } }, [], (input) => {
        const s = getState();
        if (!s.ready) throw new Error("Wait for this table to finish loading.");
        if (input.search !== undefined && (typeof input.search !== "string" || input.search.length > 200)) throw new Error("Invalid search.");
        if (input.sort_by !== undefined && input.sort_by !== "" && !s.columns.includes(input.sort_by as string)) throw new Error("Choose a current column.");
        if (input.sort_order !== undefined && input.sort_order !== "asc" && input.sort_order !== "desc") throw new Error("Invalid sort_order.");
        const pageSize = integerArgument(input.page_size, s.pageSize, 10, 100);
        if (![10, 25, 50, 100].includes(pageSize)) throw new Error("Unsupported page size.");
        const changed = input.search !== undefined || input.sort_by !== undefined || input.sort_order !== undefined || input.page_size !== undefined;
        const query = { page: integerArgument(input.page, changed ? 1 : s.page, 1, changed ? 1 : Math.max(1, s.pages)),
          pageSize, search: input.search as string ?? s.search,
          sortBy: input.sort_by === "" ? undefined : input.sort_by as string ?? s.sortBy,
          sortOrder: input.sort_order as "asc" | "desc" ?? s.sortOrder };
        s.apply(query);
        return { status: "query_requested", ...query };
      }),
    actionTool("table_open_row", "Open the detail drawer for a zero-based row index in the current ready page. Does not change the record.", false,
      { row_index: { type: "integer", minimum: 0 } }, ["row_index"], ({ row_index }) => {
        const s = getState();
        if (!s.ready || !s.rows.length) throw new Error("Wait for a populated table page.");
        const index = integerArgument(row_index, 0, 0, s.rows.length - 1);
        s.selectRow(s.rows[index]);
        return { status: "drawer_requested", row_index: index };
      }),
  ];
}
