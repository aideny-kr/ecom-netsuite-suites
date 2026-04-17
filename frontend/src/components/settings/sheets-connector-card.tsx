"use client";

import { useCallback, useState } from "react";
import {
  useMcpConnectors,
  useDeleteMcpConnector,
  useTestSheetsConnection,
  useCreateSheetsConnector,
} from "@/hooks/use-mcp-connectors";
import { usePermissions } from "@/hooks/use-permissions";
import { Button } from "@/components/ui/button";
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
  FileSpreadsheet,
  Loader2,
  Trash2,
  FlaskConical,
  CheckCircle2,
  XCircle,
  Upload,
  Wifi,
  WifiOff,
} from "lucide-react";

export function SheetsConnectorCard() {
  const { isAdmin } = usePermissions();
  const { toast } = useToast();
  const { data: mcpConnectors } = useMcpConnectors();

  const deleteMcp = useDeleteMcpConnector();
  const testConnection = useTestSheetsConnection();
  const createConnector = useCreateSheetsConnector();

  const [serviceAccountJson, setServiceAccountJson] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ valid: boolean; error?: string | null } | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const sheetsConnector = (mcpConnectors ?? []).find(
    (c) => c.provider === "google_sheets" && c.status !== "revoked",
  );

  const parseJson = useCallback((): Record<string, unknown> | null => {
    try {
      const parsed = JSON.parse(serviceAccountJson);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setJsonError("Service account JSON must be an object");
        return null;
      }
      if (!parsed.client_email || !parsed.private_key) {
        setJsonError("Missing required fields: client_email, private_key");
        return null;
      }
      setJsonError(null);
      return parsed as Record<string, unknown>;
    } catch {
      setJsonError("Invalid JSON format");
      return null;
    }
  }, [serviceAccountJson]);

  if (!isAdmin) return null;

  function handleFileUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 100_000) {
      toast({ title: "File too large", description: "Service account JSON should be under 100KB.", variant: "destructive" });
      return;
    }
    const reader = new FileReader();
    reader.onload = (event) => {
      setServiceAccountJson(event.target?.result as string);
      setJsonError(null);
      setTestResult(null);
    };
    reader.onerror = () => {
      toast({ title: "Failed to read file", variant: "destructive" });
    };
    reader.readAsText(file);
    e.target.value = "";
  }

  async function handleTest() {
    const parsed = parseJson();
    if (!parsed) return;
    try {
      const result = await testConnection.mutateAsync({ service_account_json: parsed });
      setTestResult(result);
      if (result.valid) {
        toast({ title: "Connection successful" });
      } else {
        toast({ title: "Connection failed", description: result.error ?? "Unknown error", variant: "destructive" });
      }
    } catch (err) {
      toast({ title: "Test failed", description: String(err), variant: "destructive" });
    }
  }

  async function handleSave() {
    const parsed = parseJson();
    if (!parsed) return;
    try {
      await createConnector.mutateAsync({ service_account_json: parsed });
      toast({ title: "Google Sheets connected" });
      setServiceAccountJson("");
      setTestResult(null);
      setJsonError(null);
    } catch (err) {
      toast({ title: "Failed to save connection", description: String(err), variant: "destructive" });
    }
  }

  async function handleDelete() {
    if (!deleteId) return;
    try {
      await deleteMcp.mutateAsync(deleteId);
      toast({ title: "Google Sheets disconnected" });
      setDeleteId(null);
    } catch (err) {
      toast({ title: "Failed to remove connection", description: String(err), variant: "destructive" });
    }
  }

  if (sheetsConnector) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="h-4 w-4 text-green-600" />
            <h3 className="text-[15px] font-semibold">Google Sheets</h3>
          </div>
          <Badge
            variant={sheetsConnector.status === "active" ? "default" : "destructive"}
            className="text-[11px]"
          >
            {sheetsConnector.status === "active" ? (
              <Wifi className="mr-1 h-3 w-3" />
            ) : (
              <WifiOff className="mr-1 h-3 w-3" />
            )}
            {sheetsConnector.status}
          </Badge>
        </div>
        <div className="mt-3 text-[13px] text-muted-foreground">
          Service Account:{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12px]">
            {(sheetsConnector.metadata_json?.client_email as string) ?? "—"}
          </code>
        </div>
        <div className="mt-3 flex items-center justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            onClick={() => setDeleteId(sheetsConnector.id)}
          >
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            Remove
          </Button>
        </div>
        <AlertDialog open={!!deleteId} onOpenChange={(open) => !open && setDeleteId(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Remove Google Sheets Connection</AlertDialogTitle>
              <AlertDialogDescription>
                This will disconnect Google Sheets. The agent will no longer be able to create or
                write to spreadsheets.
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

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FileSpreadsheet className="h-4 w-4 text-green-600" />
          <h3 className="text-[15px] font-semibold">Google Sheets</h3>
        </div>
        <span className="text-[13px] text-muted-foreground">Not connected</span>
      </div>
      <p className="text-[13px] text-muted-foreground">
        Connect a Google service account to export chat data to Google Sheets.
      </p>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <Label htmlFor="sheets-sa-json" className="text-[13px]">
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
          id="sheets-sa-json"
          placeholder="Paste your service account JSON key file contents here..."
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
              {testResult.valid ? "Connected" : testResult.error ?? "Connection failed"}
            </span>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={handleTest}
          disabled={testConnection.isPending || !serviceAccountJson.trim()}
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
          disabled={createConnector.isPending || !serviceAccountJson.trim() || !testResult?.valid}
        >
          {createConnector.isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" />
          )}
          Save Connection
        </Button>
      </div>
    </div>
  );
}
