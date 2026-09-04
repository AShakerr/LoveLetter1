import { NextResponse } from "next/server";
import { z } from "zod";
import { requireDriverApi } from "../_auth";
import { startTrip } from "@/lib/trips";

const body = z.object({ applicationId: z.string().min(1) });

export async function POST(req: Request) {
  const driver = await requireDriverApi();
  if (!driver) return NextResponse.json({ error: "UNAUTHORIZED" }, { status: 401 });
  const parsed = body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) return NextResponse.json({ error: "BAD_REQUEST" }, { status: 400 });
  const res = await startTrip(parsed.data.applicationId, driver.id);
  if (!res.ok) return NextResponse.json({ error: res.error }, { status: 409 });
  return NextResponse.json({ tripId: res.trip.id, startedAt: res.trip.startedAt });
}
