export interface OnboardingVideo {
  title: string;
  url: string | null;
  duration?: string;
}

export const ONBOARDING_VIDEOS: Record<string, OnboardingVideo> = {
  profile: {
    title: "Setting up your business profile",
    url: null, // TODO: Add video URL
    duration: "2 min",
  },
  connection: {
    title: "Connecting NetSuite via OAuth",
    url: null,
    duration: "3 min",
  },
  policy: {
    title: "Configuring data access policies",
    url: null,
    duration: "2 min",
  },
  workspace: {
    title: "Creating your first workspace",
    url: null,
    duration: "2 min",
  },
  first_success: {
    title: "Your first script validation",
    url: null,
    duration: "4 min",
  },
};
