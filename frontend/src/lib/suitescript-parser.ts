/**
 * SuiteScript metadata parser — extracts script type, API version,
 * module dependencies, and record context from file content and path.
 */

export type ScriptType =
  | "UserEventScript"
  | "ClientScript"
  | "ScheduledScript"
  | "MapReduceScript"
  | "Suitelet"
  | "Restlet"
  | "MassUpdateScript"
  | "WorkflowActionScript"
  | "BundleInstallationScript"
  | "Portlet"
  | "Library"
  | "Unknown";

export interface ScriptMetadata {
  scriptType: ScriptType;
  scriptTypeShort: string;
  apiVersion: string | null;
  moduleScope: string | null;
  dependencies: string[];
  /** Record types inferred from filename/content */
  recordTypes: string[];
  /** Color class for the script type badge */
  color: string;
  /** Icon hint for the script type */
  icon: string;
  /** Governance limit hint */
  governanceHint: string | null;
}

const SCRIPT_TYPE_MAP: Record<ScriptType, { short: string; color: string; icon: string; governance: string | null }> = {
  UserEventScript:          { short: "UE",    color: "bg-blue-500/15 text-blue-700 border-blue-500/30",     icon: "zap",       governance: "1,000 units" },
  ClientScript:             { short: "CS",    color: "bg-purple-500/15 text-purple-700 border-purple-500/30", icon: "monitor",   governance: null },
  ScheduledScript:          { short: "SS",    color: "bg-amber-500/15 text-amber-700 border-amber-500/30",   icon: "clock",     governance: "10,000 units" },
  MapReduceScript:          { short: "MR",    color: "bg-emerald-500/15 text-emerald-700 border-emerald-500/30", icon: "layers",  governance: "10,000 units" },
  Suitelet:                 { short: "SU",    color: "bg-cyan-500/15 text-cyan-700 border-cyan-500/30",       icon: "layout",    governance: "10,000 units" },
  Restlet:                  { short: "RL",    color: "bg-orange-500/15 text-orange-700 border-orange-500/30", icon: "globe",     governance: "5,000 units" },
  MassUpdateScript:         { short: "MU",    color: "bg-rose-500/15 text-rose-700 border-rose-500/30",       icon: "refresh",   governance: "10,000 units" },
  WorkflowActionScript:     { short: "WA",    color: "bg-indigo-500/15 text-indigo-700 border-indigo-500/30", icon: "git-branch", governance: "1,000 units" },
  BundleInstallationScript: { short: "BI",    color: "bg-pink-500/15 text-pink-700 border-pink-500/30",       icon: "package",   governance: "10,000 units" },
  Portlet:                  { short: "PT",    color: "bg-teal-500/15 text-teal-700 border-teal-500/30",       icon: "columns",   governance: "1,000 units" },
  Library:                  { short: "LIB",   color: "bg-slate-500/15 text-slate-700 border-slate-500/30",     icon: "book",      governance: null },
  Unknown:                  { short: "?",     color: "bg-gray-500/15 text-gray-500 border-gray-500/30",       icon: "file",      governance: null },
};

/** Known NetSuite record type keywords */
const RECORD_TYPE_KEYWORDS = [
  "customer", "vendor", "employee", "partner", "contact", "lead", "prospect",
  "salesorder", "purchaseorder", "invoice", "creditmemo", "vendorbill",
  "itemfulfillment", "itemreceipt", "returnauthorization", "estimate",
  "opportunity", "cashsale", "journalentry", "inventoryadjustment",
  "transferorder", "workorder", "inventoryitem", "assemblyitem",
  "lotitem", "serializeditem", "noninventoryitem", "serviceitem",
  "otherchargeitem", "discountitem", "paymentitem", "subtotalitem",
  "customrecord", "task", "phonecall", "event", "case", "issue",
  "project", "projecttask", "timebill", "expensereport",
];

/** Friendly display names for record types */
const RECORD_DISPLAY_NAMES: Record<string, string> = {
  salesorder: "Sales Order",
  purchaseorder: "Purchase Order",
  vendorbill: "Vendor Bill",
  itemfulfillment: "Item Fulfillment",
  itemreceipt: "Item Receipt",
  returnauthorization: "Return Authorization",
  creditmemo: "Credit Memo",
  cashsale: "Cash Sale",
  journalentry: "Journal Entry",
  inventoryadjustment: "Inventory Adjustment",
  transferorder: "Transfer Order",
  workorder: "Work Order",
  inventoryitem: "Inventory Item",
  assemblyitem: "Assembly Item",
  noninventoryitem: "Non-Inventory Item",
  serviceitem: "Service Item",
  customrecord: "Custom Record",
  projecttask: "Project Task",
  timebill: "Time Bill",
  expensereport: "Expense Report",
  phonecall: "Phone Call",
};

function toDisplayName(raw: string): string {
  const lower = raw.toLowerCase().replace(/[_\-\s]/g, "");
  return RECORD_DISPLAY_NAMES[lower] || raw.charAt(0).toUpperCase() + raw.slice(1);
}

export function parseSuiteScriptMetadata(content: string | null, filePath: string): ScriptMetadata {
  const result: ScriptMetadata = {
    scriptType: "Unknown",
    scriptTypeShort: "?",
    apiVersion: null,
    moduleScope: null,
    dependencies: [],
    recordTypes: [],
    color: SCRIPT_TYPE_MAP.Unknown.color,
    icon: SCRIPT_TYPE_MAP.Unknown.icon,
    governanceHint: null,
  };

  // Non-JS/TS files
  const ext = filePath.split(".").pop()?.toLowerCase();
  if (ext && !["js", "ts", "jsx", "tsx"].includes(ext)) {
    return result;
  }

  // Parse from content if available
  if (content) {
    // @NScriptType
    const scriptTypeMatch = content.match(/@NScriptType\s+(\w+)/);
    if (scriptTypeMatch) {
      const rawType = scriptTypeMatch[1];
      const matched = (Object.keys(SCRIPT_TYPE_MAP) as ScriptType[]).find(
        (k) => k.toLowerCase() === rawType.toLowerCase(),
      );
      if (matched) {
        result.scriptType = matched;
      }
    }

    // @NApiVersion
    const apiMatch = content.match(/@NApiVersion\s+([\d.]+)/);
    if (apiMatch) {
      result.apiVersion = apiMatch[1];
    }

    // @NModuleScope
    const scopeMatch = content.match(/@NModuleScope\s+(\w+)/);
    if (scopeMatch) {
      result.moduleScope = scopeMatch[1];
    }

    // define([...]) dependencies
    const defineMatch = content.match(/define\s*\(\s*\[([^\]]*)\]/);
    if (defineMatch) {
      result.dependencies = defineMatch[1]
        .split(",")
        .map((d) => d.trim().replace(/['"]/g, ""))
        .filter(Boolean);
    }

    // Record type detection from content
    // Look for record.Type.XXXX patterns
    let m: RegExpExecArray | null;
    const recordTypeRe = /record\.Type\.([A-Z_]+)/g;
    while ((m = recordTypeRe.exec(content)) !== null) {
      const rt = m[1].toLowerCase().replace(/_/g, "");
      if (!result.recordTypes.includes(rt)) {
        result.recordTypes.push(rt);
      }
    }

    // Look for search.create({ type: 'xxx' })
    const searchTypeRe = /type\s*:\s*['"]([a-z_]+)['"]/g;
    while ((m = searchTypeRe.exec(content)) !== null) {
      const rt = m[1].toLowerCase().replace(/_/g, "");
      if (RECORD_TYPE_KEYWORDS.includes(rt) && !result.recordTypes.includes(rt)) {
        result.recordTypes.push(rt);
      }
    }
  }

  // Fallback: infer from file path
  if (result.scriptType === "Unknown") {
    const pathLower = filePath.toLowerCase();
    if (pathLower.includes("userevent") || pathLower.includes("_ue")) {
      result.scriptType = "UserEventScript";
    } else if (pathLower.includes("client") || pathLower.includes("_cs")) {
      result.scriptType = "ClientScript";
    } else if (pathLower.includes("scheduled") || pathLower.includes("_ss")) {
      result.scriptType = "ScheduledScript";
    } else if (pathLower.includes("mapreduce") || pathLower.includes("_mr")) {
      result.scriptType = "MapReduceScript";
    } else if (pathLower.includes("suitelet") || pathLower.includes("_su")) {
      result.scriptType = "Suitelet";
    } else if (pathLower.includes("restlet") || pathLower.includes("_rl")) {
      result.scriptType = "Restlet";
    } else if (pathLower.includes("util") || pathLower.includes("lib") || pathLower.includes("helper")) {
      result.scriptType = "Library";
    }
  }

  // Infer record type from filename
  if (result.recordTypes.length === 0) {
    const fileName = filePath.split("/").pop()?.toLowerCase() || "";
    for (const rt of RECORD_TYPE_KEYWORDS) {
      if (fileName.includes(rt)) {
        result.recordTypes.push(rt);
        break;
      }
    }
  }

  // Apply type metadata
  const meta = SCRIPT_TYPE_MAP[result.scriptType];
  result.scriptTypeShort = meta.short;
  result.color = meta.color;
  result.icon = meta.icon;
  result.governanceHint = meta.governance;

  // Friendly record type names
  result.recordTypes = result.recordTypes.map(toDisplayName);

  return result;
}

/** Group files by script type for constellation view */
export interface ConstellationGroup {
  scriptType: ScriptType;
  label: string;
  color: string;
  files: Array<{
    id: string;
    path: string;
    name: string;
    metadata: ScriptMetadata;
  }>;
}

// Re-use FileTreeNode from types
import type { FileTreeNode } from "@/lib/types";

export function buildConstellationGroups(
  nodes: FileTreeNode[],
  fileContents: Map<string, string>,
): ConstellationGroup[] {
  const groups = new Map<ScriptType, ConstellationGroup>();

  function walk(nodeList: FileTreeNode[]) {
    for (const node of nodeList) {
      if (node.is_directory) {
        if (node.children) walk(node.children);
        continue;
      }

      const content = fileContents.get(node.id) || null;
      const metadata = parseSuiteScriptMetadata(content, node.path);

      if (!groups.has(metadata.scriptType)) {
        const meta = SCRIPT_TYPE_MAP[metadata.scriptType];
        groups.set(metadata.scriptType, {
          scriptType: metadata.scriptType,
          label: metadata.scriptType === "Unknown" ? "Other Files" : metadata.scriptType.replace(/Script$/, " Script"),
          color: meta.color,
          files: [],
        });
      }

      groups.get(metadata.scriptType)!.files.push({
        id: node.id,
        path: node.path,
        name: node.name,
        metadata,
      });
    }
  }

  walk(nodes);

  // Sort: known types first alphabetically, Unknown last
  const order: ScriptType[] = [
    "UserEventScript", "ClientScript", "ScheduledScript", "MapReduceScript",
    "Suitelet", "Restlet", "MassUpdateScript", "WorkflowActionScript",
    "Library", "Unknown",
  ];

  const result: ConstellationGroup[] = order
    .filter((t) => groups.has(t))
    .map((t) => groups.get(t)!);

  // Add any types not in the predefined order
  groups.forEach((group, type) => {
    if (!order.includes(type)) {
      result.push(group);
    }
  });

  return result;
}

/** Get script type label for display */
export function getScriptTypeLabel(type: ScriptType): string {
  if (type === "Unknown") return "Other";
  return type.replace(/Script$/, "");
}

/** Detect file language from path */
export function detectLanguage(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "js": return "javascript";
    case "ts": return "typescript";
    case "json": return "json";
    case "xml": return "xml";
    case "html": return "html";
    case "css": return "css";
    case "sql": return "sql";
    default: return "plaintext";
  }
}
