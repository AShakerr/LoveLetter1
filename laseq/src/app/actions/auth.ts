"use server";

import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { db } from "@/lib/db";
import { normalizeEgyptPhone } from "@/lib/phone";
import { requestOtp, verifyOtp } from "@/lib/otp";
import { clearSession, createSession } from "@/lib/session";
import { homeFor } from "@/lib/auth";
import { LOCALE_COOKIE, isLocale } from "@/lib/i18n";

export type AuthState = { error?: "INVALID_PHONE" | "INVALID_CODE" | "RATE_LIMITED" } | undefined;

export async function requestOtpAction(_prev: AuthState, formData: FormData): Promise<AuthState> {
  const phone = normalizeEgyptPhone(String(formData.get("phone") ?? ""));
  if (!phone) return { error: "INVALID_PHONE" };
  const res = await requestOtp(phone);
  if (!res.ok) return { error: "RATE_LIMITED" };
  redirect(`/login/verify?phone=${encodeURIComponent(phone)}`);
}

export async function verifyOtpAction(_prev: AuthState, formData: FormData): Promise<AuthState> {
  const phone = normalizeEgyptPhone(String(formData.get("phone") ?? ""));
  const code = String(formData.get("code") ?? "").replace(/\D/g, "");
  if (!phone) return { error: "INVALID_PHONE" };
  const ok = await verifyOtp(phone, code);
  if (!ok) return { error: "INVALID_CODE" };

  const isAdmin = process.env.ADMIN_PHONE && normalizeEgyptPhone(process.env.ADMIN_PHONE) === phone;
  const user = await db.user.upsert({
    where: { phone },
    create: { phone, role: isAdmin ? "ADMIN" : null },
    update: isAdmin ? { role: "ADMIN" } : {},
  });
  await createSession({ userId: user.id, role: user.role });
  redirect(homeFor(user.role));
}

export async function logoutAction(): Promise<void> {
  await clearSession();
  redirect("/");
}

export async function setLocaleAction(formData: FormData): Promise<void> {
  const locale = formData.get("locale");
  const next = String(formData.get("next") ?? "/");
  if (isLocale(locale)) {
    const jar = await cookies();
    jar.set(LOCALE_COOKIE, locale, { path: "/", maxAge: 60 * 60 * 24 * 365, sameSite: "lax" });
  }
  redirect(next.startsWith("/") ? next : "/");
}
