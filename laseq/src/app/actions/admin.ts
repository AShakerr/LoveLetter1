"use server";

import { revalidatePath } from "next/cache";
import { db } from "@/lib/db";
import { requireRole } from "@/lib/auth";
import { clearFlaggedTrip } from "@/lib/trips";

function decision(formData: FormData): "APPROVED" | "REJECTED" {
  return String(formData.get("decision")) === "approve" ? "APPROVED" : "REJECTED";
}

export async function reviewDriverAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  await db.driverProfile.update({
    where: { id },
    data: { status: decision(formData), reviewNote: String(formData.get("note") ?? "") || null },
  });
  revalidatePath("/admin/drivers");
}

export async function reviewAdvertiserAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  await db.advertiser.update({ where: { id }, data: { status: decision(formData) } });
  revalidatePath("/admin/advertisers");
}

export async function reviewCampaignAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  const approved = decision(formData) === "APPROVED";
  await db.campaign.update({
    where: { id },
    data: { status: approved ? "ACTIVE" : "REJECTED", reviewNote: String(formData.get("note") ?? "") || null },
  });
  revalidatePath("/admin/campaigns");
}

export async function verifyInstallAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  const approved = decision(formData) === "APPROVED";
  await db.application.update({
    where: { id },
    data: approved
      ? { status: "ACTIVE", installVerifiedAt: new Date() }
      : { status: "ACCEPTED", installPhotoUrl: null, note: String(formData.get("note") ?? "") || null },
  });
  revalidatePath("/admin/installs");
}

export async function markPayoutPaidAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  const rejected = String(formData.get("decision")) === "reject";
  await db.payout.update({
    where: { id },
    data: rejected
      ? { status: "REJECTED" }
      : { status: "PAID", paidAt: new Date(), reference: String(formData.get("reference") ?? "") || null },
  });
  revalidatePath("/admin/payouts");
}

export async function resolveTripAction(formData: FormData): Promise<void> {
  await requireRole("ADMIN");
  const id = String(formData.get("id"));
  if (String(formData.get("decision")) === "approve") {
    await clearFlaggedTrip(id);
  } else {
    await db.trip.update({ where: { id }, data: { status: "COMPLETED", earningsPiasters: 0, distanceKm: 0, estImpressions: 0, flagReason: "VOIDED" } });
  }
  revalidatePath("/admin/trips");
}
