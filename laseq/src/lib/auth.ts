import "server-only";
import { redirect } from "next/navigation";
import { db } from "./db";
import { getSession } from "./session";
import type { Role } from "@/generated/prisma/enums";

export async function getCurrentUser() {
  const session = await getSession();
  if (!session) return null;
  return db.user.findUnique({
    where: { id: session.userId },
    include: { driver: { include: { vehicles: true } }, advertiser: true },
  });
}

export type CurrentUser = NonNullable<Awaited<ReturnType<typeof getCurrentUser>>>;

/** Where a logged-in user should land based on their role. */
export function homeFor(role: Role | null | undefined): string {
  switch (role) {
    case "DRIVER":
      return "/driver";
    case "ADVERTISER":
      return "/advertiser";
    case "ADMIN":
      return "/admin";
    default:
      return "/onboarding";
  }
}

export async function requireUser(): Promise<CurrentUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}

export async function requireRole(role: Role): Promise<CurrentUser> {
  const user = await requireUser();
  if (user.role !== role) redirect(homeFor(user.role));
  if (role === "DRIVER" && !user.driver) redirect("/onboarding/driver");
  if (role === "ADVERTISER" && !user.advertiser) redirect("/onboarding/advertiser");
  return user;
}
