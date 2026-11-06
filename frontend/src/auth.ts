import { createAuthClient } from "@neondatabase/auth";

const authUrl = import.meta.env.VITE_NEON_AUTH_URL;

export const authClient = authUrl ? createAuthClient(authUrl) : null;

export async function getAuthToken(): Promise<string | null> {
  if (!authClient) return null;
  const { data } = await authClient.getSession();
  return data?.session?.token ?? null;
}
