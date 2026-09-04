"use server";

import { redirect } from "next/navigation";
import { z } from "zod";
import { db } from "@/lib/db";
import { requireUser } from "@/lib/auth";
import { createSession } from "@/lib/session";
import { CITY_BY_SLUG } from "@/lib/cities";

const driverSchema = z.object({
  name: z.string().trim().min(2).max(80),
  city: z.string().refine((c) => c in CITY_BY_SLUG),
  rideHailing: z.enum(["NONE", "UBER", "CAREEM", "INDRIVE", "DIDI"]),
  avgWeeklyKm: z.coerce.number().int().min(0).max(5000),
  payoutMethod: z.enum(["VODAFONE_CASH", "INSTAPAY", "BANK_TRANSFER"]),
  payoutAccount: z.string().trim().min(3).max(60),
  licenseNumber: z.string().trim().max(40).optional(),
  nationalId: z.string().trim().max(20).optional(),
  make: z.string().trim().min(1).max(40),
  model: z.string().trim().min(1).max(40),
  year: z.coerce.number().int().min(1990).max(new Date().getFullYear() + 1),
  color: z.string().trim().min(1).max(30),
  plate: z.string().trim().min(2).max(20),
  bodyType: z.enum(["SEDAN", "HATCHBACK", "SUV", "MICROBUS", "PICKUP"]),
});

export type FormState = { error?: string } | undefined;

export async function createDriverProfileAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const user = await requireUser();
  const parsed = driverSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) return { error: parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ") };
  const d = parsed.data;
  if (user.driver) redirect("/driver");

  await db.$transaction(async (tx) => {
    await tx.user.update({ where: { id: user.id }, data: { name: d.name, role: "DRIVER" } });
    await tx.driverProfile.create({
      data: {
        userId: user.id,
        city: d.city,
        rideHailing: d.rideHailing,
        avgWeeklyKm: d.avgWeeklyKm,
        payoutMethod: d.payoutMethod,
        payoutAccount: d.payoutAccount,
        licenseNumber: d.licenseNumber || null,
        nationalId: d.nationalId || null,
        vehicles: {
          create: { make: d.make, model: d.model, year: d.year, color: d.color, plate: d.plate, bodyType: d.bodyType },
        },
      },
    });
  });
  await createSession({ userId: user.id, role: "DRIVER" });
  redirect("/driver");
}

const advertiserSchema = z.object({
  name: z.string().trim().min(2).max(80),
  companyName: z.string().trim().min(2).max(100),
  industry: z.string().trim().max(60).optional(),
  website: z.string().trim().max(120).optional(),
  taxId: z.string().trim().max(40).optional(),
});

export async function createAdvertiserAction(_prev: FormState, formData: FormData): Promise<FormState> {
  const user = await requireUser();
  const parsed = advertiserSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) return { error: parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ") };
  const d = parsed.data;
  if (user.advertiser) redirect("/advertiser");

  await db.$transaction(async (tx) => {
    await tx.user.update({ where: { id: user.id }, data: { name: d.name, role: "ADVERTISER" } });
    await tx.advertiser.create({
      data: {
        userId: user.id,
        companyName: d.companyName,
        industry: d.industry || null,
        website: d.website || null,
        taxId: d.taxId || null,
      },
    });
  });
  await createSession({ userId: user.id, role: "ADVERTISER" });
  redirect("/advertiser");
}
