"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { z } from "zod";
import { db } from "@/lib/db";
import { requireRole } from "@/lib/auth";
import { CITY_BY_SLUG } from "@/lib/cities";
import { egpToPiasters } from "@/lib/money";
import type { FormState } from "./onboarding";

const campaignSchema = z.object({
  name: z.string().trim().min(2).max(80),
  description: z.string().trim().min(10).max(1000),
  creativeUrl: z.string().trim().max(500).optional(),
  placement: z.enum(["REAR_WINDOW", "SIDE_DOORS", "FULL_WRAP"]),
  cities: z.array(z.string().refine((c) => c in CITY_BY_SLUG)).min(1),
  allowedBodyTypes: z.array(z.enum(["SEDAN", "HATCHBACK", "SUV", "MICROBUS", "PICKUP"])),
  minCarYear: z.coerce.number().int().min(1990).max(new Date().getFullYear() + 1),
  budgetEgp: z.coerce.number().min(1000),
  ratePerKmEgp: z.coerce.number().min(0.25).max(50),
  monthlyBaseEgp: z.coerce.number().min(0).max(20000),
  monthlyCapKm: z.coerce.number().int().min(100).max(10000),
  driverSlots: z.coerce.number().int().min(1).max(5000),
  startDate: z.coerce.date(),
  endDate: z.coerce.date(),
  intent: z.enum(["draft", "submit"]),
});

export async function createCampaignAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const user = await requireRole("ADVERTISER");
  const raw = {
    ...Object.fromEntries(formData),
    cities: formData.getAll("cities").map(String),
    allowedBodyTypes: formData.getAll("allowedBodyTypes").map(String),
  };
  const parsed = campaignSchema.safeParse(raw);
  if (!parsed.success) return { error: parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ") };
  const d = parsed.data;
  if (d.endDate <= d.startDate) return { error: "endDate must be after startDate" };

  const campaign = await db.campaign.create({
    data: {
      advertiserId: user.advertiser!.id,
      name: d.name,
      description: d.description,
      creativeUrl: d.creativeUrl || null,
      placement: d.placement,
      cities: JSON.stringify(d.cities),
      allowedBodyTypes: JSON.stringify(d.allowedBodyTypes),
      minCarYear: d.minCarYear,
      budgetPiasters: egpToPiasters(d.budgetEgp),
      ratePerKmPiasters: egpToPiasters(d.ratePerKmEgp),
      monthlyBasePiasters: egpToPiasters(d.monthlyBaseEgp),
      monthlyCapKm: d.monthlyCapKm,
      driverSlots: d.driverSlots,
      startDate: d.startDate,
      endDate: d.endDate,
      status: d.intent === "submit" ? "PENDING_REVIEW" : "DRAFT",
    },
  });
  redirect(`/advertiser/campaigns/${campaign.id}`);
}

async function ownCampaign(campaignId: string) {
  const user = await requireRole("ADVERTISER");
  const campaign = await db.campaign.findFirst({ where: { id: campaignId, advertiserId: user.advertiser!.id } });
  if (!campaign) redirect("/advertiser");
  return campaign;
}

export async function submitCampaignAction(formData: FormData): Promise<void> {
  const campaign = await ownCampaign(String(formData.get("campaignId")));
  if (campaign.status === "DRAFT") {
    await db.campaign.update({ where: { id: campaign.id }, data: { status: "PENDING_REVIEW" } });
  }
  revalidatePath(`/advertiser/campaigns/${campaign.id}`);
}

export async function toggleCampaignPauseAction(formData: FormData): Promise<void> {
  const campaign = await ownCampaign(String(formData.get("campaignId")));
  if (campaign.status === "ACTIVE") {
    await db.campaign.update({ where: { id: campaign.id }, data: { status: "PAUSED" } });
  } else if (campaign.status === "PAUSED") {
    await db.campaign.update({ where: { id: campaign.id }, data: { status: "ACTIVE" } });
  }
  revalidatePath(`/advertiser/campaigns/${campaign.id}`);
}

export async function decideApplicationAction(formData: FormData): Promise<void> {
  const applicationId = String(formData.get("applicationId"));
  const decision = String(formData.get("decision"));
  const user = await requireRole("ADVERTISER");
  const app = await db.application.findFirst({
    where: { id: applicationId, status: "APPLIED", campaign: { advertiserId: user.advertiser!.id } },
    include: { campaign: true },
  });
  if (!app) return;
  if (decision === "accept") {
    const filled = await db.application.count({
      where: { campaignId: app.campaignId, status: { in: ["ACCEPTED", "INSTALL_SUBMITTED", "ACTIVE"] } },
    });
    if (filled >= app.campaign.driverSlots) return;
    await db.application.update({ where: { id: applicationId }, data: { status: "ACCEPTED" } });
  } else {
    await db.application.update({ where: { id: applicationId }, data: { status: "REJECTED" } });
  }
  revalidatePath(`/advertiser/campaigns/${app.campaignId}`);
}
