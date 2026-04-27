"use client";

import { useState, useRef, useCallback } from "react";
import { Upload, FileSpreadsheet, X, Loader2 } from "lucide-react";

interface UploadedFile {
  id: string;
  name: string;
  size: number;
}

interface FileUploadZoneProps {
  onFileUploaded: (file: UploadedFile) => void;
  onFileRemoved: () => void;
  acceptedTypes?: string[];
}

export function FileUploadZone({ onFileUploaded, onFileRemoved, acceptedTypes = [".xlsx", ".csv", ".xls", ".json"] }: FileUploadZoneProps) {
  const [file, setFile] = useState<UploadedFile | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = useCallback(async (selected: File) => {
    setError(null);
    const ext = "." + selected.name.split(".").pop()?.toLowerCase();
    if (!acceptedTypes.includes(ext)) {
      setError(`Only ${acceptedTypes.join(", ")} files are accepted.`);
      return;
    }

    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", selected);
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const headers: Record<string, string> = {};
      const token = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${baseUrl}/api/v1/task-files/upload`, {
        method: "POST",
        body: formData,
        headers,
        credentials: "include",
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || "Upload failed");
      }
      const data = await response.json();
      const uploaded = { id: data.id, name: data.filename, size: data.size };
      setFile(uploaded);
      onFileUploaded(uploaded);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }, [acceptedTypes, onFileUploaded]);

  const handleRemove = () => {
    setFile(null);
    setError(null);
    onFileRemoved();
    if (inputRef.current) inputRef.current.value = "";
  };

  if (file) {
    return (
      <div className="flex items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2">
        <FileSpreadsheet className="h-4 w-4 text-primary" />
        <span className="text-[13px] text-foreground">{file.name}</span>
        <span className="text-[11px] text-muted-foreground">({(file.size / 1024).toFixed(0)} KB)</span>
        <button onClick={handleRemove} className="ml-auto text-muted-foreground hover:text-foreground">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <div>
      <button
        onClick={() => inputRef.current?.click()}
        disabled={uploading}
        className="flex items-center gap-2 rounded-lg border border-dashed border-muted-foreground/30 px-4 py-3 text-[13px] text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary disabled:opacity-50"
      >
        {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
        {uploading ? "Uploading..." : "Upload file (.xlsx, .csv, .json)"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={acceptedTypes.join(",")}
        className="hidden"
        onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])}
      />
      {error && <p className="mt-1 text-[11px] text-red-500">{error}</p>}
    </div>
  );
}
