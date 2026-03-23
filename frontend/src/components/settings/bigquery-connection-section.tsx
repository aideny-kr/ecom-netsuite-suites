"use client";

import { useState, useCallback } from "react";
import {
  useMcpConnectors,
  useDeleteMcpConnector,
  useTestBigQueryConnection,
  useCreateBigQueryConnector,
} from "@/hooks/use-mcp-connectors";
import { usePermissions } from "@/hooks/use-permissions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import {
  Database,
  Loader2,
  Trash2,
  FlaskConical,
  CheckCircle2,
  XCircle,
  Upload,
  Wifi,
  WifiOff,
} from "lucide-react";

// ---------------------------------------------------------------------------
// BigQuery Connection Section
// ---------------------------------------------------------------------------

export function BigQueryConnectionSection() {
  const { isAdmin } = usePermissions();
  const { toast } = useToast();
  const { data: mcpConnectors } = useMcpConnectors();

  // Mutations
  const deleteMcp = useDeleteMcpConnector();
  const testConnection = useTestBigQueryConnection();
  const createConnector = useCreateBigQueryConnector();

  // Form state
  const [projectId, setProjectId] = useState("");
  const [serviceAccountJson, setServiceAccountJson] = useState("");
  const [defaultDataset, setDefaultDataset] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);

  // Test result state
  const [testResult, setTestResult] = useState<{
    valid: boolean;
    datasets: string[];
    error: string | null;
  } | null>(null);

  // Delete confirmation
  const [deleteId, setDeleteId] = useState<string | null>(null);

  if (!isAdmin) return null;

  // Find existing BigQuery connector
  const bigqueryConnector = (mcpConnectors ?? []).find(
    (c) => c.provider === "bigquery" && c.status !== "revoked",
  );

  // Parse and validate JSON
  const parseServiceAccountJson = useCallback((): Record<string, unknown> | null => {
    try {
      const parsed = JSON.parse(serviceAccountJson);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setJsonError("Service account JSON must be an object");
        return null;
      }
      if (!parsed.project_id || !parsed.client_email || !parsed.private_key) {
        setJsonError("Missing required fields: project_id, client_email, private_key");
        return null;
      }
      setJsonError(null);
      return parsed as Record<string, unknown>;
    } catch {
      setJsonError("Invalid JSON format");
      return null;
    }
  }, [serviceAccountJson]);

  // Handle file upload
  function handleFileUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result as string;
      setServiceAccountJson(text);
      setJsonError(null);
      setTestResult(null);

      // Auto-fill project ID from JSON if empty
      try {
        const parsed = JSON.parse(text);
        if (parsed.project_id && !projectId) {
          setProjectId(parsed.project_id);
        }
      } catch {
        // Will be caught during validation
      }
    };
    reader.readAsText(file);
    // Reset input so the same file can be re-uploaded
    e.target.value = "";
  }

  // Test connection
  async function handleTest() {
    const parsed = parseServiceAccountJson();
    if (!parsed) return;

    if (!projectId.trim()) {
      toast({ title: "Project ID is required", variant: "destructive" });
      return;
    }

    try {
      const result = await testConnection.mutateAsync({
        project_id: projectId.trim(),
        service_account_json: parsed,
      });
      setTestResult(result);

      if (result.valid) {
        toast({ title: "Connection successful", description: `Found ${result.datasets.length} dataset(s)` });
        // Auto-select first dataset if none chosen
        if (!defaultDataset && result.datasets.length > 0) {
          setDefaultDataset(result.datasets[0]);
        }
      } else {
        toast({ title: "Connection failed", description: result.error ?? "Unknown error", variant: "destructive" });
      }
    } catch (err) {
      toast({ title: "Test failed", description: String(err), variant: "destructive" });
    }
  }

  // Save connection
  async function handleSave() {
    const parsed = parseServiceAccountJson();
    if (!parsed) return;

    if (!projectId.trim()) {
      toast({ title: "Project ID is required", variant: "destructive" });
      return;
    }

    try {
      await createConnector.mutateAsync({
        project_id: projectId.trim(),
        service_account_json: parsed,
        default_dataset: defaultDataset.trim() || undefined,
      });
      toast({ title: "BigQuery connected", description: "Connection saved successfully" });

      // Reset form
      setProjectId("");
      setServiceAccountJson("");
      setDefaultDataset("");
      setTestResult(null);
      setJsonError(null);
    } catch (err) {
      toast({ title: "Failed to save connection", description: String(err), variant: "destructive" });
    }
  }

  // Delete connection
  async function handleDelete() {
    if (!deleteId) return;
    try {
      await deleteMcp.mutateAsync(deleteId);
      toast({ title: "BigQuery disconnected" });
      setDeleteId(null);
    } catch (err) {
      toast({ title: "Failed to remove connection", description: String(err), variant: "destructive" });
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">BigQuery Connection</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Connect Google BigQuery for AI-powered analytics and dashboards
        </p>
      </div>

      {/* Existing connector card */}
      {bigqueryConnector ? (
        <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Database className="h-4 w-4 text-muted-foreground" />
              <span className="text-[13px] font-medium">BigQuery Connector</span>
            </div>
            <Badge
              variant={bigqueryConnector.status === "active" ? "default" : "destructive"}
              className="text-[11px]"
            >
              {bigqueryConnector.status === "active" ? (
                <Wifi className="mr-1 h-3 w-3" />
              ) : (
                <WifiOff className="mr-1 h-3 w-3" />
              )}
              {bigqueryConnector.status}
            </Badge>
          </div>

          {/* Connection details */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 rounded-lg border border-transparent px-3 py-2">
              <Database className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="text-[12px] font-medium text-muted-foreground shrink-0">
                Project
              </span>
              <span className="text-[13px] text-foreground font-mono">
                {(bigqueryConnector.metadata_json?.project_id as string) ?? "—"}
              </span>
            </div>
            {bigqueryConnector.metadata_json?.default_dataset ? (
              <div className="flex items-center gap-2 rounded-lg border border-transparent px-3 py-2">
                <Database className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="text-[12px] font-medium text-muted-foreground shrink-0">
                  Dataset
                </span>
                <span className="text-[13px] text-foreground font-mono">
                  {bigqueryConnector.metadata_json.default_dataset as string}
                </span>
              </div>
            ) : null}
          </div>

          {bigqueryConnector.error_reason && (
            <div className="rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2">
              <p className="text-[12px] text-destructive">{bigqueryConnector.error_reason}</p>
            </div>
          )}

          {/* Remove button */}
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={() => setDeleteId(bigqueryConnector.id)}
              disabled={deleteMcp.isPending}
            >
              {deleteMcp.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
              )}
              Remove
            </Button>
          </div>
        </div>
      ) : (
        /* Setup form */
        <div className="rounded-xl border bg-card p-6 shadow-soft space-y-5">
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-muted-foreground" />
            <span className="text-[13px] font-medium">New BigQuery Connection</span>
          </div>

          {/* Project ID */}
          <div className="space-y-1.5">
            <Label htmlFor="bq-project-id" className="text-[13px]">
              Project ID
            </Label>
            <Input
              id="bq-project-id"
              placeholder="my-gcp-project"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="text-[13px] font-mono"
            />
          </div>

          {/* Service Account JSON */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label htmlFor="bq-sa-json" className="text-[13px]">
                Service Account JSON
              </Label>
              <label className="cursor-pointer">
                <input
                  type="file"
                  accept=".json"
                  className="hidden"
                  onChange={handleFileUpload}
                />
                <span className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-muted/50">
                  <Upload className="h-3 w-3" />
                  Upload JSON
                </span>
              </label>
            </div>
            <textarea
              id="bq-sa-json"
              placeholder='Paste your service account JSON key file contents here...'
              value={serviceAccountJson}
              onChange={(e) => {
                setServiceAccountJson(e.target.value);
                setJsonError(null);
                setTestResult(null);
              }}
              className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] font-mono placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-ring min-h-[120px] resize-none"
            />
            {jsonError && (
              <p className="text-[12px] text-destructive flex items-center gap-1">
                <XCircle className="h-3 w-3" />
                {jsonError}
              </p>
            )}
          </div>

          {/* Default Dataset */}
          <div className="space-y-1.5">
            <Label htmlFor="bq-dataset" className="text-[13px]">
              Default Dataset{" "}
              <span className="text-muted-foreground font-normal">(optional)</span>
            </Label>
            {testResult?.valid && testResult.datasets.length > 0 ? (
              <select
                id="bq-dataset"
                value={defaultDataset}
                onChange={(e) => setDefaultDataset(e.target.value)}
                className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="">Select a dataset...</option>
                {testResult.datasets.map((ds) => (
                  <option key={ds} value={ds}>
                    {ds}
                  </option>
                ))}
              </select>
            ) : (
              <Input
                id="bq-dataset"
                placeholder="my_dataset"
                value={defaultDataset}
                onChange={(e) => setDefaultDataset(e.target.value)}
                className="text-[13px] font-mono"
              />
            )}
            <p className="text-[12px] text-muted-foreground">
              Test the connection first to discover available datasets
            </p>
          </div>

          {/* Test result */}
          {testResult && (
            <div
              className={`rounded-lg border px-3 py-2 ${
                testResult.valid
                  ? "border-green-500/50 bg-green-500/5"
                  : "border-destructive/50 bg-destructive/5"
              }`}
            >
              <div className="flex items-center gap-1.5">
                {testResult.valid ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-destructive" />
                )}
                <span className="text-[12px] font-medium">
                  {testResult.valid
                    ? `Connected — ${testResult.datasets.length} dataset(s) found`
                    : testResult.error ?? "Connection failed"}
                </span>
              </div>
            </div>
          )}

          {/* Action buttons */}
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleTest}
              disabled={testConnection.isPending || !projectId.trim() || !serviceAccountJson.trim()}
            >
              {testConnection.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
              )}
              Test Connection
            </Button>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={
                createConnector.isPending ||
                !projectId.trim() ||
                !serviceAccountJson.trim() ||
                !testResult?.valid
              }
            >
              {createConnector.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Database className="mr-1.5 h-3.5 w-3.5" />
              )}
              Save Connection
            </Button>
          </div>
        </div>
      )}

      {/* Delete confirmation dialog */}
      <AlertDialog open={!!deleteId} onOpenChange={(open) => !open && setDeleteId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove BigQuery Connection</AlertDialogTitle>
            <AlertDialogDescription>
              This will disconnect BigQuery and remove the stored credentials. The BI agent
              will no longer be able to query your data warehouse.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
