"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";

const INDUSTRIES = [
  "Retail / E-commerce",
  "Wholesale / Distribution",
  "SaaS / Technology",
  "Manufacturing",
  "Professional Services",
  "Healthcare",
  "Other",
];

const TEAM_SIZES = ["1-5", "6-20", "21-50", "51-200", "200+"];

interface StepProfileProps {
  onStepComplete: () => void;
}

export function StepProfile({ onStepComplete }: StepProfileProps) {
  const [industry, setIndustry] = useState("");
  const [description, setDescription] = useState("");
  const [teamSize, setTeamSize] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!industry || !description) {
      setError("Please fill in industry and business description");
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      // Create profile
      const profile = await apiClient.post<{ id: string }>("/api/v1/onboarding/profiles", {
        industry,
        business_description: description,
        team_size: teamSize || undefined,
      });
      // Confirm profile
      await apiClient.post(`/api/v1/onboarding/profiles/${profile.id}/confirm`);
      // Mark step complete
      try {
        await apiClient.post("/api/v1/onboarding/checklist/profile/complete");
      } catch {
        // Step might already be complete from confirm_profile
      }
      onStepComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save profile");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="space-y-6 p-6">
      <div>
        <h3 className="text-sm font-medium mb-2">Industry</h3>
        <div className="grid grid-cols-2 gap-2">
          {INDUSTRIES.map((ind) => (
            <button
              key={ind}
              onClick={() => setIndustry(ind)}
              className={`rounded-lg border px-3 py-2 text-sm text-left transition-colors ${
                industry === ind
                  ? "border-primary bg-primary/5 text-foreground"
                  : "border-border hover:border-primary/40 text-muted-foreground"
              }`}
            >
              {ind}
            </button>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-sm font-medium mb-2">Business Description</h3>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Tell us about your business..."
          className="w-full rounded-lg border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring min-h-[100px] resize-none"
        />
      </div>

      <div>
        <h3 className="text-sm font-medium mb-2">Team Size <span className="text-muted-foreground font-normal">(optional)</span></h3>
        <div className="flex gap-2 flex-wrap">
          {TEAM_SIZES.map((size) => (
            <button
              key={size}
              onClick={() => setTeamSize(size)}
              className={`rounded-full border px-4 py-1.5 text-xs font-medium transition-colors ${
                teamSize === size
                  ? "border-primary bg-primary/5 text-foreground"
                  : "border-border hover:border-primary/40 text-muted-foreground"
              }`}
            >
              {size}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <Button onClick={handleSubmit} disabled={!industry || !description || isSaving} className="w-full">
        {isSaving ? "Saving..." : "Save Profile"}
      </Button>
    </div>
  );
}
