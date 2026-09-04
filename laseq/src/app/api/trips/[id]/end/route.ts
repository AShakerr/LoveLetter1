import { NextResponse } from "next/server";
import { requireDriverApi } from "../../../_auth";
import { endTrip } from "@/lib/trips";

export async function POST(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const driver = await requireDriverApi();
  if (!driver) return NextResponse.json({ error: "UNAUTHORIZED" }, { status: 401 });
  const { id } = await ctx.params;
  const res = await endTrip(id, driver.id);
  if (!res.ok) return NextResponse.json({ error: res.error }, { status: 404 });
  return NextResponse.json({
    tripId: res.trip.id,
    status: res.trip.status,
    distanceKm: res.trip.distanceKm,
    estImpressions: res.trip.estImpressions,
    earningsPiasters: res.trip.earningsPiasters,
    flagReasons: res.verdict.reasons,
  });
}
