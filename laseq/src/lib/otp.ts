import "server-only";
import { db } from "./db";

const OTP_TTL_MS = 5 * 60 * 1000;
const MAX_ACTIVE_CODES = 5;

export function isDevOtp(): boolean {
  return (process.env.OTP_PROVIDER ?? "console") === "console";
}

async function deliver(phone: string, code: string): Promise<void> {
  const provider = process.env.OTP_PROVIDER ?? "console";
  switch (provider) {
    case "console":
      console.log(`[laseq/otp] code for ${phone}: ${code}`);
      return;
    // Wire up a real provider here (Twilio Verify, Vodafone/Etisalat SMS gateway,
    // or the WhatsApp Business API which has the best delivery rate in Egypt).
    default:
      throw new Error(`Unknown OTP_PROVIDER "${provider}"`);
  }
}

export async function requestOtp(phone: string): Promise<{ ok: true } | { ok: false; error: "RATE_LIMITED" }> {
  const active = await db.otpCode.count({
    where: { phone, consumed: false, expiresAt: { gt: new Date() } },
  });
  if (active >= MAX_ACTIVE_CODES) return { ok: false, error: "RATE_LIMITED" };

  const code = String(Math.floor(100000 + Math.random() * 900000));
  await db.otpCode.create({
    data: { phone, code, expiresAt: new Date(Date.now() + OTP_TTL_MS) },
  });
  await deliver(phone, code);
  return { ok: true };
}

export async function verifyOtp(phone: string, code: string): Promise<boolean> {
  const row = await db.otpCode.findFirst({
    where: { phone, code, consumed: false, expiresAt: { gt: new Date() } },
    orderBy: { createdAt: "desc" },
  });
  if (!row) return false;
  await db.otpCode.updateMany({ where: { phone, consumed: false }, data: { consumed: true } });
  return true;
}
