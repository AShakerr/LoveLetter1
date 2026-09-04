import { getCurrentUser } from "@/lib/auth";

/** Resolve the calling driver from the session cookie. A mobile app would send a bearer token instead. */
export async function requireDriverApi() {
  const user = await getCurrentUser();
  if (!user || user.role !== "DRIVER" || !user.driver) return null;
  return user.driver;
}
