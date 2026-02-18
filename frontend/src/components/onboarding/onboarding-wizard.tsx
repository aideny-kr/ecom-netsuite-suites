"use client";

import { useState, useCallback, useEffect } from "react";
import { apiClient } from "@/lib/api-client";
import type { OnboardingChecklist, OnboardingChecklistItem } from "@/lib/types";
import { WizardHeader } from "@/components/onboarding/wizard-header";
import { WizardFooter } from "@/components/onboarding/wizard-footer";
import { ChatCopilotPanel } from "@/components/onboarding/chat-copilot-panel";
import { MicroVideoModal } from "@/components/onboarding/micro-video-modal";
import { StepProfile } from "@/components/onboarding/steps/step-profile";
import { StepConnection } from "@/components/onboarding/steps/step-connection";
import { StepPolicy } from "@/components/onboarding/steps/step-policy";
import { StepWorkspace } from "@/components/onboarding/steps/step-workspace";
import { StepFirstSuccess } from "@/components/onboarding/steps/step-first-success";
import { ONBOARDING_VIDEOS } from "@/lib/onboarding-videos";

const STEP_KEYS = ["profile", "connection", "policy", "workspace", "first_success"];

interface OnboardingWizardProps {
  onComplete: () => void;
}

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
  const [currentStep, setCurrentStep] = useState(0);
  const [checklist, setChecklist] = useState<OnboardingChecklistItem[]>([]);
  const [isValidating, setIsValidating] = useState(false);
  const [videoStep, setVideoStep] = useState<string | null>(null);
  const [isExiting, setIsExiting] = useState(false);

  const fetchChecklist = useCallback(async () => {
    try {
      const data = await apiClient.get<OnboardingChecklist>("/api/v1/onboarding/checklist");
      setChecklist(data.items);
    } catch (err) {
      console.error("Failed to fetch checklist:", err);
    }
  }, []);

  useEffect(() => {
    fetchChecklist();
  }, [fetchChecklist]);

  const handleStepComplete = useCallback(() => {
    fetchChecklist();
    if (currentStep < STEP_KEYS.length - 1) {
      setCurrentStep((s) => s + 1);
    }
  }, [currentStep, fetchChecklist]);

  const handleNext = useCallback(() => {
    if (currentStep < STEP_KEYS.length - 1) {
      setCurrentStep((s) => s + 1);
    }
  }, [currentStep]);

  const handleBack = useCallback(() => {
    if (currentStep > 0) {
      setCurrentStep((s) => s - 1);
    }
  }, [currentStep]);

  const handleSkip = useCallback(async () => {
    const stepKey = STEP_KEYS[currentStep];
    try {
      await apiClient.post(`/api/v1/onboarding/checklist/${stepKey}/skip`);
      await fetchChecklist();
      if (currentStep < STEP_KEYS.length - 1) {
        setCurrentStep((s) => s + 1);
      }
    } catch (err) {
      console.error("Failed to skip step:", err);
    }
  }, [currentStep, fetchChecklist]);

  const handleDone = useCallback(async () => {
    setIsValidating(true);
    try {
      await apiClient.post("/api/v1/onboarding/finalize");
      setIsExiting(true);
      setTimeout(onComplete, 500);
    } catch (err) {
      console.error("Failed to finalize:", err);
    } finally {
      setIsValidating(false);
    }
  }, [onComplete]);

  const handleDismiss = useCallback(() => {
    localStorage.setItem("onboarding_skipped", "true");
    setIsExiting(true);
    setTimeout(onComplete, 500);
  }, [onComplete]);

  const handleVideoClick = useCallback((stepKey: string) => {
    const video = ONBOARDING_VIDEOS[stepKey];
    if (video?.url) {
      setVideoStep(stepKey);
    }
  }, []);

  const currentStepKey = STEP_KEYS[currentStep];
  const currentItem = checklist.find((c) => c.step_key === currentStepKey);
  const canProceed = currentItem?.status === "completed" || currentItem?.status === "skipped";

  const renderStep = () => {
    switch (currentStepKey) {
      case "profile":
        return <StepProfile onStepComplete={handleStepComplete} />;
      case "connection":
        return <StepConnection onStepComplete={handleStepComplete} />;
      case "policy":
        return <StepPolicy onStepComplete={handleStepComplete} />;
      case "workspace":
        return <StepWorkspace onStepComplete={handleStepComplete} />;
      case "first_success":
        return <StepFirstSuccess onStepComplete={handleStepComplete} />;
      default:
        return null;
    }
  };

  return (
    <div
      className={`fixed inset-0 z-50 flex transition-all duration-500 ${
        isExiting ? "opacity-0 scale-105" : "opacity-100 scale-100 animate-in fade-in duration-300"
      }`}
      style={{
        background: "linear-gradient(135deg, hsl(250 40% 15%) 0%, hsl(230 35% 20%) 30%, hsl(260 30% 18%) 60%, hsl(240 35% 12%) 100%)",
      }}
    >
      <div className="m-4 flex flex-1 overflow-hidden rounded-2xl border border-white/10 bg-background shadow-2xl">
        {/* Left panel -- Wizard (60%) */}
        <div className="flex w-[60%] flex-col">
          <WizardHeader
            currentStep={currentStep}
            checklist={checklist}
            onVideoClick={handleVideoClick}
          />

          {/* Step content */}
          <div className="flex-1 overflow-auto">{renderStep()}</div>

          <WizardFooter
            currentStep={currentStep}
            totalSteps={STEP_KEYS.length}
            canProceed={canProceed}
            isValidating={isValidating}
            onBack={handleBack}
            onNext={handleNext}
            onSkip={handleSkip}
            onDone={handleDone}
          />

          {/* Dismiss button */}
          <div className="px-6 pb-3 text-center">
            <button
              onClick={handleDismiss}
              className="text-xs text-muted-foreground/60 hover:text-muted-foreground transition-colors"
            >
              Set up later
            </button>
          </div>
        </div>

        {/* Right panel -- Chat Copilot (40%) */}
        <div className="w-[40%]">
          <ChatCopilotPanel wizardStep={currentStepKey} />
        </div>
      </div>

      <MicroVideoModal
        stepKey={videoStep}
        open={!!videoStep}
        onOpenChange={(open) => !open && setVideoStep(null)}
      />
    </div>
  );
}
