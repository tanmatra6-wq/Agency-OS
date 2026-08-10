"use client";

import React, { createContext, useContext, useState, useEffect } from "react";

export interface KnownTenant {
  tenantId: string;
  tenantName: string;
  brandId: string;
  brandName: string;
}

interface TenantContextType {
  tenantId: string;
  setTenantId: (id: string) => void;
  activeBrandId: string | null;
  setActiveBrandId: (id: string | null) => void;
  role: string;
  setRole: (role: string) => void;
  operatorToken: string;
  setOperatorToken: (token: string) => Promise<void>;
  sessionToken: string;
  knownTenants: KnownTenant[];
  addKnownTenant: (tenantId: string, tenantName: string, brandId: string, brandName: string) => void;
}

const TenantContext = createContext<TenantContextType | undefined>(undefined);

const DEFAULT_TENANTS: KnownTenant[] = [
  {
    tenantId: "t1",
    tenantName: "Bootstrap Developer",
    brandId: "brand-bootstrap",
    brandName: "Bootstrap Brand"
  }
];

export function TenantProvider({ children }: { children: React.ReactNode }) {
  const [tenantId, setTenantIdState] = useState<string>("t1");
  const [activeBrandId, setActiveBrandIdState] = useState<string | null>("brand-bootstrap");
  const [role, setRoleState] = useState<string>("AGENCY_OWNER"); // default dev role
  const [operatorToken, setOperatorTokenState] = useState<string>("");
  const [sessionToken, setSessionTokenState] = useState<string>("");
  const [knownTenants, setKnownTenants] = useState<KnownTenant[]>(DEFAULT_TENANTS);

  // Load from localStorage on client boot
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- Loaded on client mount to prevent SSR hydration mismatch */
    const savedTenant = localStorage.getItem("aos_tenant_id");
    if (savedTenant) {
      setTenantIdState(savedTenant);
    }
    const savedBrand = localStorage.getItem("aos_brand_id");
    if (savedBrand) {
      setActiveBrandIdState(savedBrand);
    }
    const savedRole = localStorage.getItem("aos_role");
    if (savedRole) {
      setRoleState(savedRole);
    }
    const savedToken = localStorage.getItem("aos_operator_token");
    if (savedToken) {
      setOperatorTokenState(savedToken);
    }
    const savedSessionToken = localStorage.getItem("aos_session_token");
    if (savedSessionToken) {
      setSessionTokenState(savedSessionToken);
    }

    const savedKnownTenants = localStorage.getItem("aos_known_tenants");
    if (savedKnownTenants) {
      try {
        setKnownTenants(JSON.parse(savedKnownTenants));
      } catch {
        setKnownTenants(DEFAULT_TENANTS);
      }
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  // Fetch tenants from backend on boot or when token changes
  useEffect(() => {
    const fetchTenants = async () => {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      // GET /tenants is operator-gated; attach the session bearer token or raw operator token if set.
      const token = sessionToken || operatorToken;
      try {
        const res = await fetch(`${baseUrl}/tenants`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error(`HTTP error ${res.status}`);
        const data = await res.json();
        const mapped = data.map((t: { tenant_id: string; tenant_name: string; brand_id: string; brand_name: string }) => ({
          tenantId: t.tenant_id,
          tenantName: t.tenant_name,
          brandId: t.brand_id,
          brandName: t.brand_name
        }));
        
        if (mapped.length > 0) {
          setKnownTenants(mapped);
          localStorage.setItem("aos_known_tenants", JSON.stringify(mapped));
          
          // Auto-select the first real tenant if current is default/unset
          const savedTenant = localStorage.getItem("aos_tenant_id");
          if (!savedTenant || savedTenant === "t1") {
            const firstReal = mapped[0];
            setTenantIdState(firstReal.tenantId);
            setActiveBrandIdState(firstReal.brandId);
            localStorage.setItem("aos_tenant_id", firstReal.tenantId);
            localStorage.setItem("aos_brand_id", firstReal.brandId);
          }
        }
      } catch (error) {
        console.error("Failed to fetch tenants from backend, falling back to local storage:", error);
      }
    };
    
    fetchTenants();
  }, [sessionToken, operatorToken]);

  const setTenantId = (id: string) => {
    setTenantIdState(id);
    localStorage.setItem("aos_tenant_id", id);
    
    // Also auto-select the first brand of this tenant if found in known tenants
    const tenantObj = knownTenants.find(t => t.tenantId === id);
    if (tenantObj) {
      setActiveBrandId(tenantObj.brandId);
    }
  };

  const setActiveBrandId = (id: string | null) => {
    setActiveBrandIdState(id);
    if (id) {
      localStorage.setItem("aos_brand_id", id);
    } else {
      localStorage.removeItem("aos_brand_id");
    }
  };

  const setRole = (r: string) => {
    setRoleState(r);
    localStorage.setItem("aos_role", r);
  };

  const setOperatorToken = async (token: string) => {
    setOperatorTokenState(token);
    if (!token) {
      setSessionTokenState("");
      localStorage.removeItem("aos_session_token");
      localStorage.removeItem("aos_operator_token");
      return;
    }
    
    // Store raw operator token (mostly for dev debugging / easy reload)
    localStorage.setItem("aos_operator_token", token);
    
    // Bootstrap session on backend
    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const res = await fetch(`${baseUrl}/session/bootstrap`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operator_token: token })
      });
      if (!res.ok) throw new Error("Invalid operator token");
      const data = await res.json();
      setSessionTokenState(data.session_token);
      localStorage.setItem("aos_session_token", data.session_token);
    } catch (err) {
      console.error("Session bootstrap failed, falling back to raw token:", err);
      setSessionTokenState(token);
      localStorage.setItem("aos_session_token", token);
    }
  };

  const addKnownTenant = (tId: string, tName: string, bId: string, bName: string) => {
    setKnownTenants(prev => {
      // Prevent duplicates
      if (prev.some(t => t.tenantId === tId)) return prev;
      const updated = [...prev, { tenantId: tId, tenantName: tName, brandId: bId, brandName: bName }];
      localStorage.setItem("aos_known_tenants", JSON.stringify(updated));
      return updated;
    });
  };

  return (
    <TenantContext.Provider value={{ 
      tenantId, 
      setTenantId, 
      activeBrandId, 
      setActiveBrandId, 
      role,
      setRole,
      operatorToken,
      setOperatorToken,
      sessionToken,
      knownTenants,
      addKnownTenant
    }}>
      {children}
    </TenantContext.Provider>
  );
}

export function useTenant() {
  const context = useContext(TenantContext);
  if (context === undefined) {
    throw new Error("useTenant must be used within a TenantProvider");
  }
  return context;
}
