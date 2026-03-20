"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { Zap } from "lucide-react";
import dynamic from "next/dynamic";
const GoogleLogin = dynamic(
  () => import("@react-oauth/google").then((m) => m.GoogleLogin),
  { ssr: false },
);
import { apiClient } from "@/lib/api-client";
import { Loader2 } from "lucide-react";

export default function LoginPage() {
  const { login } = useAuth();
  const { toast } = useToast();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isGoogleLoading, setIsGoogleLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);
    try {
      await login({ email, password });
    } catch (err) {
      toast({
        title: "Login failed",
        description:
          err instanceof Error ? err.message : "Invalid credentials",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen dark bg-[#0e0e0e]">
      {/* Left Panel - Branding */}
      <div className="hidden lg:flex lg:w-[480px] lg:flex-col lg:justify-between bg-zinc-950 p-10 text-white border-r border-zinc-800/50">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-[#ff7936]">
            <Zap className="h-5 w-5 text-black" />
          </div>
          <span className="text-lg font-bold tracking-tight">
            Suite Studio AI
          </span>
        </div>
        <div>
          <h2 className="text-4xl font-black leading-none tracking-tight">
            STREAMLINE
            <br />
            <span className="text-[#ff7936]">YOUR OPS_</span>
          </h2>
          <p className="mt-6 text-[15px] leading-relaxed text-zinc-500">
            Connect Shopify, Stripe, and NetSuite in one unified platform.
            Automate data syncing, reconciliation, and journal postings.
          </p>
        </div>
        <p className="text-[10px] text-zinc-700 uppercase tracking-widest">
          © Suite Studio AI — Modular Precision
        </p>
      </div>

      {/* Right Panel - Form */}
      <div className="flex flex-1 items-center justify-center px-6 bg-[#0e0e0e]">
        <div className="w-full max-w-[380px]">
          {/* Mobile logo */}
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <div className="flex h-9 w-9 items-center justify-center rounded-sm bg-[#ff7936]">
              <Zap className="h-5 w-5 text-black" />
            </div>
            <span className="text-lg font-bold tracking-tight text-white">
              Suite Studio AI
            </span>
          </div>

          <div className="mb-8">
            <h1 className="text-2xl font-bold tracking-tight text-white">
              Sign in
            </h1>
            <p className="mt-1.5 text-[15px] text-zinc-500">
              Enter your credentials to access your account
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="email" className="text-[13px] font-medium text-zinc-400 uppercase tracking-wider">
                Email
              </Label>
              <Input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="h-11 bg-zinc-900 border-zinc-800 text-white rounded-sm placeholder:text-zinc-600 focus:ring-[#ff7936] focus:border-[#ff7936]"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-[13px] font-medium text-zinc-400 uppercase tracking-wider">
                Password
              </Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="h-11 bg-zinc-900 border-zinc-800 text-white rounded-sm placeholder:text-zinc-600 focus:ring-[#ff7936] focus:border-[#ff7936]"
              />
            </div>
            <button
              type="submit"
              className="h-11 w-full text-[14px] font-bold uppercase tracking-widest bg-[#ff7936] text-black rounded-sm hover:bg-[#ff915d] transition-all disabled:opacity-50"
              disabled={isLoading}
            >
              {isLoading ? "Signing in..." : "Sign in"}
            </button>
          </form>

          <div className="relative my-6">
            <div className="absolute inset-0 flex items-center">
              <span className="w-full border-t border-zinc-800" />
            </div>
            <div className="relative flex justify-center text-xs uppercase">
              <span className="bg-[#0e0e0e] px-2 text-zinc-600 tracking-widest">or</span>
            </div>
          </div>

          {isGoogleLoading ? (
            <Button
              variant="outline"
              className="h-11 w-full text-[14px] font-medium"
              disabled
            >
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Signing in with Google...
            </Button>
          ) : (
            <div className="flex justify-center">
              <GoogleLogin
                onSuccess={async (credentialResponse) => {
                  if (!credentialResponse.credential) return;
                  setIsGoogleLoading(true);
                  try {
                    const res = await apiClient.post<{ access_token: string; refresh_token: string }>(
                      "/api/v1/auth/google",
                      { google_id_token: credentialResponse.credential },
                    );
                    localStorage.setItem("access_token", res.access_token);
                    document.cookie = `access_token=${res.access_token}; path=/; max-age=604800; samesite=lax`;
                    window.location.href = "/chat";
                  } catch (err) {
                    toast({
                      title: "Google sign-in failed",
                      description: err instanceof Error ? err.message : "Could not sign in with Google",
                      variant: "destructive",
                    });
                  } finally {
                    setIsGoogleLoading(false);
                  }
                }}
                onError={() => {
                  toast({
                    title: "Google sign-in failed",
                    description: "Google authentication was cancelled or failed",
                    variant: "destructive",
                  });
                }}
                text="signin_with"
                shape="rectangular"
                width={380}
              />
            </div>
          )}

          <p className="mt-6 text-center text-[13px] text-zinc-500">
            Don&apos;t have an account?{" "}
            <Link
              href="/register"
              className="font-medium text-[#ff7936] hover:underline"
            >
              Create one
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
