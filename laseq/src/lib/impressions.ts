import { CITY_BY_SLUG } from "./cities";

/**
 * Time-of-day multiplier. Cairo rush hours (8-10, 15-19 local) are when the
 * most eyeballs are on the road; late night traffic is thin.
 * Hours are in Africa/Cairo local time.
 */
export function hourMultiplier(hourLocal: number): number {
  if (hourLocal >= 8 && hourLocal < 10) return 1.4;
  if (hourLocal >= 15 && hourLocal < 19) return 1.5;
  if (hourLocal >= 10 && hourLocal < 15) return 1.1;
  if (hourLocal >= 19 && hourLocal < 23) return 0.9;
  return 0.4; // 23:00 - 08:00
}

/** Placement affects how visible the creative is. */
export function placementMultiplier(placement: "REAR_WINDOW" | "SIDE_DOORS" | "FULL_WRAP"): number {
  switch (placement) {
    case "REAR_WINDOW":
      return 1.0;
    case "SIDE_DOORS":
      return 1.35;
    case "FULL_WRAP":
      return 1.9;
  }
}

export function cairoHour(date: Date): number {
  const h = new Intl.DateTimeFormat("en-GB", {
    hour: "numeric",
    hour12: false,
    timeZone: "Africa/Cairo",
  }).format(date);
  return Number.parseInt(h, 10) % 24;
}

/**
 * Estimate impressions for a segment driven in `citySlug`.
 * Falls back to a conservative 250/km outside the known cities.
 */
export function estimateImpressions(params: {
  distanceKm: number;
  citySlug?: string | null;
  at: Date;
  placement: "REAR_WINDOW" | "SIDE_DOORS" | "FULL_WRAP";
}): number {
  const base = params.citySlug ? CITY_BY_SLUG[params.citySlug]?.impressionsPerKm ?? 250 : 250;
  const est =
    params.distanceKm * base * hourMultiplier(cairoHour(params.at)) * placementMultiplier(params.placement);
  return Math.max(0, Math.round(est));
}

/**
 * Rough monthly reach for a campaign, used on the advertiser "plan" screen.
 * Assumes each driver does `avgWeeklyKm` km/week (capped by the campaign cap).
 */
export function estimateMonthlyCampaignImpressions(params: {
  driverSlots: number;
  avgWeeklyKm: number;
  monthlyCapKm: number;
  citySlugs: string[];
  placement: "REAR_WINDOW" | "SIDE_DOORS" | "FULL_WRAP";
}): number {
  const monthlyKm = Math.min(params.avgWeeklyKm * 4.3, params.monthlyCapKm);
  const perKm =
    params.citySlugs.length === 0
      ? 250
      : params.citySlugs.reduce((s, c) => s + (CITY_BY_SLUG[c]?.impressionsPerKm ?? 250), 0) /
        params.citySlugs.length;
  // Blend of hour multipliers over a typical driving day ≈ 1.1
  return Math.round(params.driverSlots * monthlyKm * perKm * 1.1 * placementMultiplier(params.placement));
}

/** CPM (cost per 1,000 impressions) in piasters, for the advertiser dashboard. */
export function cpmPiasters(spendPiasters: number, impressions: number): number {
  if (impressions <= 0) return 0;
  return Math.round((spendPiasters / impressions) * 1000);
}
