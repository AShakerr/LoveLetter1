import "server-only";
import { db } from "./db";
import { isInsideAnyCity, parseCities, CITIES } from "./cities";
import { estimateImpressions } from "./impressions";
import { monthStart, tripEarningsPiasters } from "./earnings";
import { screenTrip, summarizePings } from "./geo";

export async function startTrip(applicationId: string, driverId: string) {
  const app = await db.application.findFirst({
    where: { id: applicationId, driverId, status: "ACTIVE", campaign: { status: "ACTIVE" } },
  });
  if (!app) return { ok: false as const, error: "NOT_ACTIVE" as const };
  const open = await db.trip.findFirst({ where: { applicationId, status: "ACTIVE" } });
  if (open) return { ok: true as const, trip: open };
  const trip = await db.trip.create({ data: { applicationId } });
  return { ok: true as const, trip };
}

export async function addPings(
  tripId: string,
  driverId: string,
  pings: { lat: number; lng: number; speedKmh?: number | null; recordedAt?: string | number | Date }[],
) {
  const trip = await db.trip.findFirst({
    where: { id: tripId, status: "ACTIVE", application: { driverId } },
  });
  if (!trip) return { ok: false as const, error: "NOT_FOUND" as const };
  if (pings.length === 0) return { ok: true as const, count: 0 };
  await db.locationPing.createMany({
    data: pings.map((p) => ({
      tripId,
      lat: p.lat,
      lng: p.lng,
      speedKmh: p.speedKmh ?? null,
      recordedAt: p.recordedAt ? new Date(p.recordedAt) : new Date(),
    })),
  });
  return { ok: true as const, count: pings.length };
}

/** Which campaign city a coordinate is in, or null. */
function cityAt(lat: number, lng: number, citySlugs: string[]): string | null {
  for (const slug of citySlugs) if (isInsideAnyCity(lat, lng, [slug])) return slug;
  for (const c of CITIES) if (isInsideAnyCity(lat, lng, [c.slug])) return c.slug;
  return null;
}

/**
 * Close a trip: compute distance from the ping trail, count only km inside the
 * campaign's cities, estimate impressions, compute earnings against the monthly
 * cap, and run the fraud screen. Flagged trips earn nothing until an admin clears them.
 */
export async function endTrip(tripId: string, driverId: string) {
  const trip = await db.trip.findFirst({
    where: { id: tripId, status: "ACTIVE", application: { driverId } },
    include: {
      pings: { orderBy: { recordedAt: "asc" } },
      application: { include: { campaign: true } },
    },
  });
  if (!trip) return { ok: false as const, error: "NOT_FOUND" as const };

  const campaign = trip.application.campaign;
  const citySlugs = parseCities(campaign.cities);
  const summary = summarizePings(trip.pings);

  // Per-segment accounting so that impressions reflect the city and hour of each km.
  let paidKm = 0;
  let impressions = 0;
  let outsidePings = 0;
  for (let i = 1; i < trip.pings.length; i++) {
    const a = trip.pings[i - 1];
    const b = trip.pings[i];
    const seg = summarizePings([a, b]);
    if (seg.distanceKm <= 0) continue;
    const city = cityAt(b.lat, b.lng, citySlugs);
    const inCampaignCity = city !== null && citySlugs.includes(city as never);
    if (!inCampaignCity) {
      outsidePings++;
      continue;
    }
    paidKm += seg.distanceKm;
    impressions += estimateImpressions({
      distanceKm: seg.distanceKm,
      citySlug: city,
      at: b.recordedAt,
      placement: campaign.placement,
    });
  }

  const endedAt = new Date();
  // Duration comes from the ping trail itself, not wall-clock: a phone that was
  // offline may upload a whole trip's pings seconds before ending it.
  const first = trip.pings[0]?.recordedAt ?? trip.startedAt;
  const lastPing = trip.pings[trip.pings.length - 1]?.recordedAt ?? endedAt;
  const durationHours = Math.max(lastPing.getTime() - first.getTime(), 0) / 3_600_000;
  const verdict = screenTrip({
    summary,
    durationHours,
    pingCount: trip.pings.length,
    outsideCityRatio: trip.pings.length > 1 ? outsidePings / (trip.pings.length - 1) : 0,
  });

  const since = monthStart(endedAt);
  const agg = await db.trip.aggregate({
    where: { applicationId: trip.applicationId, status: "COMPLETED", endedAt: { gte: since } },
    _sum: { distanceKm: true },
  });
  const kmAlreadyThisMonth = agg._sum.distanceKm ?? 0;

  const earnings = verdict.flagged
    ? 0
    : tripEarningsPiasters({
        distanceKm: paidKm,
        ratePerKmPiasters: campaign.ratePerKmPiasters,
        monthlyCapKm: campaign.monthlyCapKm,
        kmAlreadyThisMonth,
      });

  const updated = await db.trip.update({
    where: { id: tripId },
    data: {
      endedAt,
      distanceKm: Math.round(paidKm * 100) / 100,
      estImpressions: impressions,
      earningsPiasters: earnings,
      status: verdict.flagged ? "FLAGGED" : "COMPLETED",
      flagReason: verdict.flagged ? verdict.reasons.join(",") : null,
    },
  });
  return { ok: true as const, trip: updated, verdict };
}

/** Admin clears a flagged trip: recompute earnings and mark completed. */
export async function clearFlaggedTrip(tripId: string) {
  const trip = await db.trip.findUnique({
    where: { id: tripId },
    include: { application: { include: { campaign: true } } },
  });
  if (!trip || trip.status !== "FLAGGED") return;
  const since = monthStart(trip.endedAt ?? new Date());
  const agg = await db.trip.aggregate({
    where: { applicationId: trip.applicationId, status: "COMPLETED", endedAt: { gte: since } },
    _sum: { distanceKm: true },
  });
  const earnings = tripEarningsPiasters({
    distanceKm: trip.distanceKm,
    ratePerKmPiasters: trip.application.campaign.ratePerKmPiasters,
    monthlyCapKm: trip.application.campaign.monthlyCapKm,
    kmAlreadyThisMonth: agg._sum.distanceKm ?? 0,
  });
  await db.trip.update({
    where: { id: tripId },
    data: { status: "COMPLETED", earningsPiasters: earnings, flagReason: null },
  });
}

export async function driverStats(driverId: string) {
  const [earned, paid, trips] = await Promise.all([
    db.trip.aggregate({
      where: { application: { driverId }, status: "COMPLETED" },
      _sum: { earningsPiasters: true, distanceKm: true, estImpressions: true },
    }),
    db.payout.aggregate({
      where: { driverId, status: { in: ["REQUESTED", "PAID"] } },
      _sum: { amountPiasters: true },
    }),
    db.trip.count({ where: { application: { driverId }, status: "COMPLETED" } }),
  ]);
  const totalEarned = earned._sum.earningsPiasters ?? 0;
  const totalPaid = paid._sum.amountPiasters ?? 0;
  return {
    totalEarned,
    balance: totalEarned - totalPaid,
    totalKm: earned._sum.distanceKm ?? 0,
    totalImpressions: earned._sum.estImpressions ?? 0,
    tripCount: trips,
  };
}

export async function campaignStats(campaignId: string) {
  const [agg, activeCars, applicants] = await Promise.all([
    db.trip.aggregate({
      where: { application: { campaignId }, status: "COMPLETED" },
      _sum: { earningsPiasters: true, distanceKm: true, estImpressions: true },
    }),
    db.application.count({ where: { campaignId, status: "ACTIVE" } }),
    db.application.count({ where: { campaignId, status: { in: ["APPLIED", "ACCEPTED", "INSTALL_SUBMITTED", "ACTIVE"] } } }),
  ]);
  return {
    driverEarningsPiasters: agg._sum.earningsPiasters ?? 0,
    km: agg._sum.distanceKm ?? 0,
    impressions: agg._sum.estImpressions ?? 0,
    activeCars,
    applicants,
  };
}
