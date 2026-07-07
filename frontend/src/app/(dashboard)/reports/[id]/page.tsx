"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { AlertTriangle, ArrowLeft, Download, PauseCircle, RefreshCw } from "lucide-react";
import {
  useRefreshReport,
  useReport,
  useReportVersions,
  useResumeAutoRefresh,
  useUpdateReportSettings,
  type AutoRefreshInterval,
} from "@/hooks/use-reports";

function fmtStamp(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function ReportViewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [html, setHtml] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null = the current version (the stable /view URL); a number = a historical snapshot.
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  // bumped after a successful refresh so the effect re-fetches the (new) current HTML.
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const { data: report } = useReport(id);
  const { data: versions } = useReportVersions(id);
  const refresh = useRefreshReport(id);
  const updateSettings = useUpdateReportSettings(id);
  const resume = useResumeAutoRefresh(id);
  const viewingCurrent = selectedVersion === null;
  const paused = Boolean(report?.auto_refresh_paused_at);
  const refreshFailing = (report?.refresh_failure_count ?? 0) > 0;

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    const path =
      selectedVersion === null
        ? `/api/v1/reports/${id}/view`
        : `/api/v1/reports/${id}/versions/${selectedVersion}/view`;
    apiClient
      .getText(path)
      .then((reportHtml) => {
        if (cancelled) return;
        setHtml(reportHtml);
        url = URL.createObjectURL(new Blob([reportHtml], { type: "text/html" }));
        setBlobUrl((old) => {
          if (old) URL.revokeObjectURL(old); // never leak the previous blob
          return url;
        });
      })
      .catch(() => !cancelled && setError("Report not found"));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id, selectedVersion, refreshNonce]);

  // Save the frozen artifact as a standalone .html file — the publishable page.
  // Deliberately NOT "open blob in new tab": a blob: URL shares this app's origin,
  // which would reintroduce the injection surface the empty-sandbox iframe closes;
  // a downloaded file opens as file:// with no app cookies or storage.
  function handleDownload() {
    if (!html) return;
    const dl = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    const a = document.createElement("a");
    a.href = dl;
    a.download = `report-${id}.html`;
    a.click();
    URL.revokeObjectURL(dl);
  }

  function handleRefresh() {
    setActionMsg(null);
    refresh.mutate(undefined, {
      onSuccess: () => {
        setSelectedVersion(null); // a refresh always lands you on the new current version
        setRefreshNonce((n) => n + 1);
      },
      // The backend's detail strings are user-facing ("refreshed recently — try again
      // in about Ns" on the debounce; "reconnect"-style messages on source failures).
      // The last good iframe stays untouched on any error.
      onError: (e: Error) => setActionMsg(e.message || "Refresh failed"),
    });
  }

  const stampSource = report?.last_refreshed_at ?? report?.created_at;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 border-b-[3px] border-black bg-card px-4 py-2">
        <Button variant="ghost" size="sm" onClick={() => router.back()}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back
        </Button>
        {stampSource && (
          <span className="text-[13px] text-muted-foreground">Data as of {fmtStamp(stampSource)}</span>
        )}
        {actionMsg && <span className="text-[13px] text-destructive">{actionMsg}</span>}
        <div className="ml-auto flex items-center gap-2">
          {versions && versions.length > 1 && (
            <select
              aria-label="Report version"
              className="h-8 rounded-md border bg-background px-2 text-[13px]"
              value={selectedVersion === null ? "current" : String(selectedVersion)}
              onChange={(e) =>
                setSelectedVersion(e.target.value === "current" ? null : Number(e.target.value))
              }
            >
              {versions.map((v) => (
                <option key={v.version} value={v.is_current ? "current" : String(v.version)}>
                  v{v.version}
                  {v.is_current ? " · current" : ""} · {fmtStamp(v.created_at)}
                </option>
              ))}
            </select>
          )}
          {report?.has_recipe && (
            <select
              aria-label="Auto-refresh interval"
              className="h-8 rounded-md border bg-background px-2 text-[13px]"
              value={report.auto_refresh ?? "daily"}
              disabled={updateSettings.isPending}
              onChange={(e) => updateSettings.mutate(e.target.value as AutoRefreshInterval)}
            >
              <option value="off">Auto-refresh: off</option>
              <option value="hourly">Auto-refresh: hourly</option>
              <option value="daily">Auto-refresh: daily</option>
            </select>
          )}
          {report?.has_recipe && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={refresh.isPending || !viewingCurrent}
            >
              <RefreshCw className={`h-4 w-4 mr-1${refresh.isPending ? " animate-spin" : ""}`} />
              Refresh
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={handleDownload} disabled={!html}>
            <Download className="h-4 w-4 mr-1" />
            Download HTML
          </Button>
        </div>
      </div>
      {/* Failure-ladder banners (§4C): the last good version stays up — never a broken
          page. Copy is provider-generic: no structured failure attribution exists, so
          "check the data connection" instead of naming NetSuite. */}
      {paused ? (
        <div className="flex items-center gap-2.5 border-b border-amber-500/30 bg-amber-500/5 px-4 py-2.5">
          <PauseCircle className="h-4 w-4 text-amber-600 shrink-0" />
          <span className="flex-1 text-[13px]">
            Auto-refresh is paused after repeated failures
            {stampSource && <> — data as of {fmtStamp(stampSource)}</>}. Fix this report&apos;s data
            connection, then resume.
          </span>
          <Button variant="outline" size="sm" onClick={() => resume.mutate()} disabled={resume.isPending}>
            Resume auto-refresh
          </Button>
        </div>
      ) : refreshFailing ? (
        <div className="flex items-center gap-2.5 border-b border-amber-500/30 bg-amber-500/5 px-4 py-2.5">
          <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0" />
          <span className="text-[13px]">
            Automatic refresh has been failing
            {stampSource && <> — data as of {fmtStamp(stampSource)}</>}. Check this report&apos;s data
            connection.
          </span>
        </div>
      ) : null}
      {error ? (
        <div className="p-8 text-muted-foreground">{error}</div>
      ) : blobUrl ? (
        // sandbox="" is the most restrictive (null origin, no script/forms/popups): the
        // report is static HTML+CSS+inline SVG, and a blob: URL otherwise inherits this
        // page's origin, so any HTML/SVG injection in rendered_html would run same-origin.
        <iframe
          src={blobUrl}
          title="Report"
          sandbox=""
          className="flex-1 w-full border-0"
        />
      ) : (
        <div className="p-8 text-muted-foreground">Loading…</div>
      )}
    </div>
  );
}
