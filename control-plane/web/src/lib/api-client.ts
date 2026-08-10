import { useTenant } from "@/contexts/TenantContext";
import { paths } from "@/types/api";

type Path = keyof paths;

type ResponseOf<P extends Path, M extends string> = 
  M extends keyof paths[P]
    ? paths[P][M] extends { responses: { 200: { content: { "application/json": infer R } } } }
      ? R
      : paths[P][M] extends { responses: { 201: { content: { "application/json": infer R } } } }
        ? R
        : unknown
    : unknown;

type BodyOf<P extends Path, M extends string> = 
  M extends keyof paths[P]
    ? paths[P][M] extends { requestBody?: { content: { "application/json": infer B } } }
      ? B
      : never
    : never;

export function useApi() {
  const { tenantId, sessionToken, operatorToken } = useTenant();
  const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  async function request<
    P extends Path,
    M extends "get" | "post" | "put" | "delete"
  >(
    path: P,
    method: M,
    body?: BodyOf<P, M>,
    headers?: Record<string, string>
  ): Promise<ResponseOf<P, M>> {
    const url = `${baseUrl}${path}`;
    const reqHeaders: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Tenant-ID": tenantId,
      // Operator-gated endpoints require the bearer token; include it when set.
      ...((sessionToken || operatorToken) ? { Authorization: `Bearer ${sessionToken || operatorToken}` } : {}),
      ...headers,
    };

    const res = await fetch(url, {
      method: method.toUpperCase(),
      headers: reqHeaders,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `HTTP error ${res.status}`);
    }

    return res.json();
  }

  return { request };
}
