"use client";

import { useRef, useState } from "react";
import { Upload, FileSpreadsheet, X, Loader2 } from "lucide-react";

interface TemplateFile {
  id: string;
  name: string;
  size: number;
}

interface TemplateSlotProps {
  template: TemplateFile | null;
  onUpload: (file: TemplateFile) => void;
  onRemove: () => void;
}

export function TemplateSlot({ template, onUpload, onRemove }: TemplateSlotProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const handleFile = async (file: File) => {
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
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
      if (!response.ok) throw new Error("Upload failed");
      const data = await response.json();
      onUpload({ id: data.id, name: data.filename, size: data.size });
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex items-center gap-3 border-b bg-blue-50/30 dark:bg-blue-950/10 px-4 py-2">
      <span className="text-[11px] font-medium text-blue-700 dark:text-blue-300">Template:</span>
      {template ? (
        <div className="flex items-center gap-2">
          <FileSpreadsheet className="h-3.5 w-3.5 text-blue-600" />
          <span className="text-[12px] text-foreground">{template.name}</span>
          <button onClick={onRemove} className="text-muted-foreground hover:text-foreground">
            <X className="h-3 w-3" />
          </button>
        </div>
      ) : (
        <button
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
          className="text-[11px] text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          {uploading ? "Uploading..." : "Upload your file (optional)"}
        </button>
      )}
      <input
        ref={inputRef}
        type="file"
        accept=".xlsx,.csv,.xls,.json"
        className="hidden"
        onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])}
      />
    </div>
  );
}
