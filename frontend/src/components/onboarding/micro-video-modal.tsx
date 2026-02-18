"use client";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ONBOARDING_VIDEOS } from "@/lib/onboarding-videos";

interface MicroVideoModalProps {
  stepKey: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function MicroVideoModal({ stepKey, open, onOpenChange }: MicroVideoModalProps) {
  const video = stepKey ? ONBOARDING_VIDEOS[stepKey] : null;

  if (!video || !video.url) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{video.title}</DialogTitle>
        </DialogHeader>
        <div className="aspect-video w-full overflow-hidden rounded-lg bg-muted">
          <iframe
            src={video.url}
            title={video.title}
            className="h-full w-full"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
