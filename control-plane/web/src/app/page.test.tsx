import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DashboardLayout from "./(dashboard)/layout";
import OpsPage from "./(dashboard)/ops/page";

// Mock Tenant Context
let mockRole = "AGENCY_OWNER";
let mockTenantId = "t1";
let mockKnownTenants = [
  { tenantId: "t1", tenantName: "Bootstrap Developer", brandId: "brand-bootstrap", brandName: "Bootstrap Brand" }
];
let mockOperatorToken = "";
const mockSetRole = vi.fn().mockImplementation((val) => {
  mockRole = val;
});
const mockSetTenantId = vi.fn().mockImplementation((val) => {
  mockTenantId = val;
});
const mockSetOperatorToken = vi.fn().mockImplementation((val) => {
  mockOperatorToken = val;
});
const mockAddKnownTenant = vi.fn().mockImplementation((tId, tName, bId, bName) => {
  mockKnownTenants.push({ tenantId: tId, tenantName: tName, brandId: bId, brandName: bName });
});

vi.mock("@/contexts/TenantContext", () => ({
  useTenant: () => ({
    tenantId: mockTenantId,
    setTenantId: mockSetTenantId,
    activeBrandId: "brand-bootstrap",
    setActiveBrandId: vi.fn(),
    role: mockRole,
    setRole: mockSetRole,
    operatorToken: mockOperatorToken,
    setOperatorToken: mockSetOperatorToken,
    knownTenants: mockKnownTenants,
    addKnownTenant: mockAddKnownTenant,
  }),
}));

// Mock Next.js Navigation
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/ops",
  useRouter: () => ({ push: mockPush, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// Mock API client
const mockRequest = vi.fn();
vi.mock("@/lib/api-client", () => ({
  useApi: () => ({ request: mockRequest }),
}));

const CATALOG = {
  actions: [
    {
      name: "provision_web_host",
      title: "Provision Web Host",
      description: "domain to host",
      domain: "provision",
      parameters: { properties: { domain: { type: "STRING", description: "domain to host" } }, required: ["domain"] },
    },
  ],
};

describe("Operator Actions panel (explicit controls, no chat)", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    mockRole = "AGENCY_OWNER";
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
  });

  const renderWithProviders = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <DashboardLayout>
          <OpsPage />
        </DashboardLayout>
      </QueryClientProvider>
    );

  it("renders the actions panel and submits a structured action to /actions", async () => {
    mockRequest.mockImplementation((path: string, method?: string) => {
      if (path === "/ops") return Promise.resolve([]);
      if (path === "/connections") return Promise.resolve([]);
      if (path === "/circuit-breakers") return Promise.resolve([]);
      if (path === "/audit/events") return Promise.resolve([]);
      if (path === "/audit/verify") return Promise.resolve({ ok: true });
      if (path === "/actions/catalog" && method === "get") return Promise.resolve(CATALOG);
      if (path === "/actions" && method === "post") {
        return Promise.resolve({
          cards: [{ op_id: "op-1", action: "provision.web_host.create", state: "AWAITING_APPROVAL", preview: "plan", cost_estimate: "2500.00 INR/mo", violations: [] }],
        });
      }
      return Promise.resolve(null);
    });

    renderWithProviders();

    expect(screen.getByRole("heading", { name: /Operator Actions/i })).toBeTruthy();

    // The catalog action button renders, then opens a modal on click.
    const actionBtn = await screen.findByRole("button", { name: /Provision Web Host/i });
    fireEvent.click(actionBtn);

    const input = await screen.findByPlaceholderText(/domain to host/i);
    fireEvent.change(input, { target: { value: "ableys.in" } });
    fireEvent.click(screen.getByRole("button", { name: /Propose Action/i }));

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/actions", "post", {
        tool: "provision_web_host",
        brand_id: "brand-bootstrap",
        params: { domain: "ableys.in" },
      });
    });
  });

  it("disables actions and hides approve/reject for BRAND_VIEWER", async () => {
    mockRole = "BRAND_VIEWER";
    mockRequest.mockImplementation((path: string, method?: string) => {
      if (path === "/ops") return Promise.resolve([
        { op_id: "op-v", action: "grow.bid.adjust", state: "AWAITING_APPROVAL", domain: "grow", brand_id: "b1", preview: "x", cost_estimate: "100 INR" },
      ]);
      if (path === "/connections") return Promise.resolve([]);
      if (path === "/circuit-breakers") return Promise.resolve([]);
      if (path === "/audit/events") return Promise.resolve([]);
      if (path === "/audit/verify") return Promise.resolve({ ok: true });
      if (path === "/actions/catalog" && method === "get") return Promise.resolve(CATALOG);
      return Promise.resolve(null);
    });

    renderWithProviders();

    // Action button renders but is disabled for the read-only role.
    const actionBtn = await screen.findByRole("button", { name: /Provision Web Host/i });
    expect((actionBtn as HTMLButtonElement).disabled).toBe(true);

    // Operations queue shows no approve/reject for the viewer.
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    });
  });
});


import Home from "./page";

describe("Home Onboarding Page", () => {
  beforeEach(() => {
    mockRole = "AGENCY_OWNER";
    mockTenantId = "t1";
    mockKnownTenants = [
      { tenantId: "t1", tenantName: "Bootstrap Developer", brandId: "brand-bootstrap", brandName: "Bootstrap Brand" }
    ];
    mockOperatorToken = "";
    vi.clearAllMocks();
  });

  it("renders the onboarding screen when backend says not onboarded", async () => {
    // Mock readyz returning onboarded: false
    mockRequest.mockResolvedValueOnce({ status: "ready", onboarded: false });

    render(<Home />);

    // Renders "Loading..." initially
    expect(screen.getByText("Loading workspace context...")).toBeTruthy();

    // Renders the Onboarding Form elements once resolved
    const welcomeHeading = await screen.findByText("Welcome to Agency-OS");
    expect(welcomeHeading).toBeTruthy();
    expect(screen.getByPlaceholderText("Paste your OPERATOR_TOKEN")).toBeTruthy();
    expect(screen.getByPlaceholderText("e.g. Trending Media Group")).toBeTruthy();
    expect(screen.getByPlaceholderText("e.g. Ableys Retail")).toBeTruthy();
  });

  it("redirects to /twin when backend says already onboarded", async () => {
    // Mock readyz returning onboarded: true
    mockRequest.mockResolvedValueOnce({ status: "ready", onboarded: true });

    render(<Home />);

    // Should redirect to /twin via router.push
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("/twin");
    });
  });

  it("submits the form to /tenants and transitions to onboarded", async () => {
    mockRequest.mockImplementation((path: string, method?: string, _body?: unknown) => {
      if (path === "/readyz" && method === "get") {
        return Promise.resolve({ status: "ready", onboarded: false });
      }
      if (path === "/tenants" && method === "post") {
        return Promise.resolve({ tenant_id: "tenant-xyz", brand_id: "brand-123" });
      }
      return Promise.resolve(null);
    });

    render(<Home />);

    // Fill form
    const tokenInput = await screen.findByPlaceholderText("Paste your OPERATOR_TOKEN");
    fireEvent.change(tokenInput, { target: { value: "my-secret-op-token" } });

    const tenantInput = screen.getByPlaceholderText("e.g. Trending Media Group");
    fireEvent.change(tenantInput, { target: { value: "My New Tenant" } });

    const brandInput = screen.getByPlaceholderText("e.g. Ableys Retail");
    fireEvent.change(brandInput, { target: { value: "My Brand" } });

    // Submit
    const submitBtn = screen.getByRole("button", { name: /Onboard & Launch Console/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/tenants", "post", {
        name: "My New Tenant",
        brand_name: "My Brand"
      });
      expect(mockAddKnownTenant).toHaveBeenCalledWith("tenant-xyz", "My New Tenant", "brand-123", "My Brand");
      expect(mockSetTenantId).toHaveBeenCalledWith("tenant-xyz");
      expect(mockPush).toHaveBeenCalledWith("/twin");
    });
  });
});

