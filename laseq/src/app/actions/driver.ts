"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { z } from "zod";
import { db } from "@/lib/db";
import { requireRole } from "@/lib/auth";
import { checkEligibility } from "@/lib/eligibility";
import { driverStats } from "@/lib/trips";
import { egpToPiasters } from "@/lib/money";

export type ActionResult = { ok: boolean; error?: string };

export async function applyToCampaignAction(formData: FormData): Promise<void> {
  const user = await requireRole("DRIVER");
  const driver = user.driver!;
  const campaignId = String(formData.get("campaignId") ?? "");
  const vehicleId = String(formData.get("vehicleId") ?? driver.vehicles[0]?.id ?? "");
  const vehicle = driver.vehicles.find((v) => v.id === vehicleId);
  const campaign = await db.campaign.findFirst({ where: { id: campaignId, status: "ACTIVE" } });
  if (!campaign || !vehicle) redirect("/driver/campaigns");

  const filled = await db.application.count({
    where: { campaignId, status: { in: ["ACCEPTED", "INSTALL_SUBMITTED", "ACTIVE"] } },
  });
  const reasons = checkEligibility({ driver, vehicle, campaign, filledSlots: filled });
  if (reasons.length > 0) redirect(`/driver/campaigns/${campaignId}`);

  await db.application.upsert({
    where: { campaignId_driverId: { campaignId, driverId: driver.id } },
    create: { campaignId, driverId: driver.id, vehicleId },
    update: {},
  });
  revalidatePath("/driver");
  redirect(`/driver/campaigns/${campaignId}`);
}

const MAX_UPLOAD = 5 * 1024 * 1024;

export async function submitInstallPhotoAction(formData: FormData): Promise<void> {
  const user = await requireRole("DRIVER");
  const applicationId = String(formData.get("applicationId") ?? "");
  const file = formData.get("photo");
  const app = await db.application.findFirst({
    where: { id: applicationId, driverId: user.driver!.id, status: { in: ["ACCEPTED", "INSTALL_SUBMITTED"] } },
  });
  if (!app || !(file instanceof File) || file.size === 0 || file.size > MAX_UPLOAD) {
    redirect("/driver/applications");
  }
  const ext = file.type === "image/png" ? "png" : "jpg";
  const fileName = `install-${applicationId}-${Date.now()}.${ext}`;
  const { writeFile, mkdir } = await import("node:fs/promises");
  const path = await import("node:path");
  const dir = path.join(process.cwd(), "public", "uploads");
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, fileName), Buffer.from(await file.arrayBuffer()));

  await db.application.update({
    where: { id: applicationId },
    data: { installPhotoUrl: `/uploads/${fileName}`, status: "INSTALL_SUBMITTED" },
  });
  revalidatePath("/driver/applications");
  redirect("/driver/applications");
}

const MIN_PAYOUT_PIASTERS = egpToPiasters(100);

export async function requestPayoutAction(_prev: ActionResult | undefined, formData: FormData): Promise<ActionResult> {
  const user = await requireRole("DRIVER");
  const driver = user.driver!;
  const amountEgp = z.coerce.number().positive().safeParse(formData.get("amount"));
  if (!amountEgp.success) return { ok: false, error: "INVALID_AMOUNT" };
  const amount = egpToPiasters(amountEgp.data);
  const stats = await driverStats(driver.id);
  if (amount < MIN_PAYOUT_PIASTERS) return { ok: false, error: "BELOW_MIN" };
  if (amount > stats.balance) return { ok: false, error: "INSUFFICIENT" };
  await db.payout.create({
    data: {
      driverId: driver.id,
      amountPiasters: amount,
      method: driver.payoutMethod,
      account: driver.payoutAccount ?? "",
    },
  });
  revalidatePath("/driver/earnings");
  return { ok: true };
}
