"use client";

import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight, Check } from "lucide-react";

interface WizardFooterProps {
  currentStep: number;
  totalSteps: number;
  canProceed: boolean;
  isValidating: boolean;
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
  onDone: () => void;
}

export function WizardFooter({
  currentStep,
  totalSteps,
  canProceed,
  isValidating,
  onBack,
  onNext,
  onSkip,
  onDone,
}: WizardFooterProps) {
  const isLastStep = currentStep === totalSteps - 1;

  return (
    <div className="border-t px-6 py-4 flex items-center justify-between">
      <Button
        variant="ghost"
        size="sm"
        onClick={onBack}
        disabled={currentStep === 0}
        className="gap-1"
      >
        <ChevronLeft className="h-4 w-4" />
        Back
      </Button>

      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onSkip} className="text-muted-foreground">
          Skip
        </Button>
        {isLastStep ? (
          <Button size="sm" onClick={onDone} disabled={!canProceed || isValidating} className="gap-1">
            <Check className="h-4 w-4" />
            {isValidating ? "Finalizing..." : "Done"}
          </Button>
        ) : (
          <Button size="sm" onClick={onNext} disabled={isValidating} className="gap-1">
            Next
            <ChevronRight className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
