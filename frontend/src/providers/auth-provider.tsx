"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { useRouter } from "next/navigation";
import { apiClient } from "@/lib/api-client";
import type {
  AuthResponse,
  LoginRequest,
  RegisterRequest,
  TenantSummary,
  User,
} from "@/lib/types";

interface AuthContextType {
  user: User | null;
  tenants: TenantSummary[];
  isLoading: boolean;
  login: (data: LoginRequest) => Promise<void>;
  register: (data: RegisterRequest) => Promise<void>;
  switchTenant: (tenantId: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

function setTokens(tokens: AuthResponse) {
  localStorage.setItem("access_token", tokens.access_token);
  localStorage.setItem("refresh_token", tokens.refresh_token);
  // Also set cookie so Next.js middleware can check auth
  document.cookie = `access_token=${tokens.access_token}; path=/; max-age=${60 * 60 * 24 * 7}; samesite=lax`;
}

function clearTokens() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  document.cookie = "access_token=; path=/; max-age=0";
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  const loadTenants = useCallback(async () => {
    try {
      const list = await apiClient.get<TenantSummary[]>("/api/v1/auth/me/tenants");
      setTenants(list);
    } catch {
      setTenants([]);
    }
  }, []);

  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (token) {
      Promise.all([
        apiClient.get<User>("/api/v1/auth/me").then(setUser),
        apiClient.get<TenantSummary[]>("/api/v1/auth/me/tenants").then(setTenants),
      ])
        .catch(() => {
          clearTokens();
        })
        .finally(() => setIsLoading(false));
    } else {
      setIsLoading(false);
    }
  }, []);

  const login = useCallback(
    async (data: LoginRequest) => {
      const res = await apiClient.post<AuthResponse>(
        "/api/v1/auth/login",
        data,
      );
      setTokens(res);
      const profile = await apiClient.get<User>("/api/v1/auth/me");
      setUser(profile);
      await loadTenants();
      router.push("/dashboard");
    },
    [router, loadTenants],
  );

  const register = useCallback(
    async (data: RegisterRequest) => {
      const res = await apiClient.post<AuthResponse>(
        "/api/v1/auth/register",
        data,
      );
      setTokens(res);
      const profile = await apiClient.get<User>("/api/v1/auth/me");
      setUser(profile);
      await loadTenants();
      router.push("/dashboard");
    },
    [router, loadTenants],
  );

  const switchTenant = useCallback(
    async (tenantId: string) => {
      const res = await apiClient.post<AuthResponse>(
        "/api/v1/auth/switch-tenant",
        { tenant_id: tenantId },
      );
      setTokens(res);
      const profile = await apiClient.get<User>("/api/v1/auth/me");
      setUser(profile);
      router.push("/dashboard");
    },
    [router],
  );

  const logout = useCallback(() => {
    clearTokens();
    setUser(null);
    router.push("/login");
  }, [router]);

  return (
    <AuthContext.Provider value={{ user, tenants, isLoading, login, register, switchTenant, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
