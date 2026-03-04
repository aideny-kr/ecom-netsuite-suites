"use client";

import {
  createContext,
  useContext,
  useEffect,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useAuth } from "@/providers/auth-provider";

interface BrandingData {
  brand_name: string | null;
  brand_color_hsl: string | null;
  brand_logo_url: string | null;
  brand_favicon_url: string | null;
  custom_domain: string | null;
  domain_verified: boolean;
}

interface BrandingContextValue {
  brandName: string;
  brandColor: string | null;
  logoUrl: string | null;
  faviconUrl: string | null;
  isLoaded: boolean;
}

const DEFAULT_BRAND_NAME = "Suite Studio AI";

const BrandingContext = createContext<BrandingContextValue>({
  brandName: DEFAULT_BRAND_NAME,
  brandColor: null,
  logoUrl: null,
  faviconUrl: null,
  isLoaded: false,
});

export function BrandingProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();

  const { data, isSuccess } = useQuery<BrandingData>({
    queryKey: ["branding"],
    queryFn: () => apiClient.get<BrandingData>("/api/v1/settings/branding"),
    enabled: !!user,
    staleTime: Infinity,
    refetchOnWindowFocus: true,
  });

  // Inject --primary CSS variable override when brand color is set
  useEffect(() => {
    if (data?.brand_color_hsl) {
      document.documentElement.style.setProperty(
        "--primary",
        data.brand_color_hsl,
      );
    } else {
      document.documentElement.style.removeProperty("--primary");
    }
    return () => {
      document.documentElement.style.removeProperty("--primary");
    };
  }, [data?.brand_color_hsl]);

  // Dynamic favicon
  useEffect(() => {
    if (data?.brand_favicon_url) {
      let link = document.querySelector(
        'link[rel="icon"]',
      ) as HTMLLinkElement | null;
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        document.head.appendChild(link);
      }
      link.href = data.brand_favicon_url;
    }
  }, [data?.brand_favicon_url]);

  // Dynamic document title
  useEffect(() => {
    if (data?.brand_name) {
      document.title = data.brand_name;
    }
  }, [data?.brand_name]);

  const value: BrandingContextValue = {
    brandName: data?.brand_name || DEFAULT_BRAND_NAME,
    brandColor: data?.brand_color_hsl || null,
    logoUrl: data?.brand_logo_url || null,
    faviconUrl: data?.brand_favicon_url || null,
    isLoaded: isSuccess,
  };

  return (
    <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>
  );
}

export function useBranding() {
  return useContext(BrandingContext);
}
