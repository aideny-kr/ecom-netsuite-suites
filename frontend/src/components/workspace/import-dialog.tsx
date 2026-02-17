"use client";

import { useState, useRef } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Upload } from "lucide-react";
import { useImportWorkspace } from "@/hooks/use-workspace";

interface ImportDialogProps {
  workspaceId: string;
}

export function ImportDialog({ workspaceId }: ImportDialogProps) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const importMutation = useImportWorkspace();

  const handleImport = async () => {
    if (!file) return;
    try {
      await importMutation.mutateAsync({ workspaceId, file });
      setOpen(false);
      setFile(null);
    } catch {
      // error handled by mutation
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" className="h-8 text-[12px]">
          <Upload className="mr-1.5 h-3 w-3" />
          Import
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Import Files</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div
            onClick={() => fileRef.current?.click()}
            className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed p-8 hover:border-primary/50 hover:bg-accent/50"
          >
            <Upload className="h-8 w-8 text-muted-foreground" />
            <p className="text-[13px] text-muted-foreground">
              {file ? file.name : "Click to select a .zip file"}
            </p>
            <input
              ref={fileRef}
              type="file"
              accept=".zip"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleImport}
              disabled={!file || importMutation.isPending}
            >
              {importMutation.isPending ? "Importing..." : "Import"}
            </Button>
          </div>
          {importMutation.isError && (
            <p className="text-[12px] text-destructive">
              {importMutation.error.message}
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
