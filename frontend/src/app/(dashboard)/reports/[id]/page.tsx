"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Download } from "lucide-react";

export default function ReportViewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [html, setHtml] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    apiClient
      .getText(`/api/v1/reports/${id}/view`)
      .then((reportHtml) => {
        if (cancelled) return;
        setHtml(reportHtml);
        url = URL.createObjectURL(new Blob([reportHtml], { type: "text/html" }));
        setBlobUrl(url);
      })
      .catch(() => !cancelled && setError("Report not found"));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id]);

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

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 border-b-[3px] border-black bg-card px-4 py-2">
        <Button variant="ghost" size="sm" onClick={() => router.back()}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back
        </Button>
        <div className="ml-auto">
          <Button variant="outline" size="sm" onClick={handleDownload} disabled={!html}>
            <Download className="h-4 w-4 mr-1" />
            Download HTML
          </Button>
        </div>
        {/* Slice 2: Publish to Drive / Download PDF buttons (disabled here) */}
      </div>
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
