"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useApi } from "@/lib/api-client";
import { useTenant } from "@/contexts/TenantContext";
import { Button } from "@/components/ui/button";
import { Bot } from "lucide-react";

export default function Home() {
  const router = useRouter();
  const { request } = useApi();
  const { tenantId, setTenantId, knownTenants, addKnownTenant, operatorToken, setOperatorToken } = useTenant();
  
  // Onboarding & Tenant Creation state
  const [newTenantName, setNewTenantName] = useState("");
  const [newBrandName, setNewBrandName] = useState("");
  const [createTenantLoading, setCreateTenantLoading] = useState(false);
  const [createTenantError, setCreateTenantError] = useState<string | null>(null);
  const [showDashboardOverride, setShowDashboardOverride] = useState(false);
  const [isOnboarded, setIsOnboarded] = useState<boolean | null>(null);

  // Fetch actual database onboarding status on mount
  useEffect(() => {
    const checkOnboarded = async () => {
      try {
        const res = await request("/readyz", "get") as { status: string; onboarded: boolean };
        setIsOnboarded(res.onboarded);
      } catch (err) {
        console.error("Failed to query backend onboarding status:", err);
        // Fallback to local storage heuristics
        setIsOnboarded(knownTenants.length > 1 || tenantId !== "t1");
      }
    };
    checkOnboarded();
  }, [request, knownTenants.length, tenantId]);

  const isFirstRun = isOnboarded === false;

  // Redirect to dashboard twin page once onboarded
  useEffect(() => {
    if (isOnboarded === true || showDashboardOverride) {
      router.push("/twin");
    }
  }, [isOnboarded, showDashboardOverride, router]);

  const handleCreateTenant = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTenantName.trim() || !newBrandName.trim()) return;

    setCreateTenantLoading(true);
    setCreateTenantError(null);
    try {
      const res = await request("/tenants", "post", {
        name: newTenantName.trim(),
        brand_name: newBrandName.trim()
      }) as { tenant_id: string; brand_id: string };
      
      addKnownTenant(res.tenant_id, newTenantName.trim(), res.brand_id, newBrandName.trim());
      setTenantId(res.tenant_id);
      setIsOnboarded(true);
      
      setNewTenantName("");
      setNewBrandName("");
      // Onboarding success will trigger the useEffect redirect
    } catch (err: unknown) {
      setCreateTenantError(err instanceof Error ? err.message : "Failed to create tenant");
    } finally {
      setCreateTenantLoading(false);
    }
  };

  if (isFirstRun && !showDashboardOverride) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-zinc-950 text-zinc-50 font-sans p-4">
        <div className="max-w-md w-full bg-zinc-900/40 border border-zinc-900 rounded-xl p-8 space-y-6 shadow-2xl backdrop-blur-sm">
          <div className="space-y-2 text-center">
            <div className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-950 border border-emerald-800 text-emerald-400 mb-2">
              <Bot className="h-5 w-5" />
            </div>
            <h1 className="text-lg font-bold tracking-tight text-zinc-100">Welcome to Agency-OS</h1>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Before launching the governance console, bootstrap your operator workspace by creating your tenant namespace and first brand.
            </p>
          </div>

          <form onSubmit={handleCreateTenant} className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-[10px] uppercase tracking-wider text-zinc-400 font-bold">Operator Token</label>
              <input
                type="password"
                required
                placeholder="Paste your OPERATOR_TOKEN"
                value={operatorToken}
                onChange={(e) => setOperatorToken(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:outline-none focus:border-zinc-700 font-mono"
              />
              <p className="text-[9px] text-zinc-600 leading-relaxed">Authenticates operator actions. Stored in this browser only.</p>
            </div>

            <div className="space-y-1.5">
              <label className="text-[10px] uppercase tracking-wider text-zinc-400 font-bold">Tenant Name</label>
              <input 
                type="text"
                required
                placeholder="e.g. Trending Media Group"
                value={newTenantName}
                onChange={(e) => setNewTenantName(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:outline-none focus:border-zinc-700 font-mono"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-[10px] uppercase tracking-wider text-zinc-400 font-bold">First Brand Name</label>
              <input 
                type="text"
                required
                placeholder="e.g. Ableys Retail"
                value={newBrandName}
                onChange={(e) => setNewBrandName(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:outline-none focus:border-zinc-700 font-mono"
              />
            </div>

            {createTenantError && (
              <p className="text-[11px] text-red-400 font-mono text-center bg-red-950/20 border border-red-900/30 py-2 rounded">
                {createTenantError}
              </p>
            )}

            <Button
              type="submit"
              disabled={createTenantLoading}
              className="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-xs h-9 rounded-lg disabled:opacity-50 transition-colors"
            >
              {createTenantLoading ? "Bootstrapping Workspace..." : "Onboard & Launch Console"}
            </Button>
          </form>

          <div className="relative flex py-2 items-center">
            <div className="flex-grow border-t border-zinc-900"></div>
            <span className="flex-shrink mx-4 text-[9px] uppercase tracking-widest text-zinc-600 font-bold">Or</span>
            <div className="flex-grow border-t border-zinc-900"></div>
          </div>

          <div className="text-center">
            <button
              onClick={() => setShowDashboardOverride(true)}
              className="text-[10px] text-zinc-500 hover:text-zinc-300 font-mono underline transition-colors"
            >
              Explore Developer Sandbox (t1) &rarr;
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950 text-zinc-400 font-mono text-xs">
      Loading workspace context...
    </div>
  );
}
