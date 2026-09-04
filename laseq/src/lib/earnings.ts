/**
 * Driver earnings for a trip.
 * Drivers are paid per km up to the campaign's monthly cap; the campaign's
 * monthly base (if any) is paid separately when a full month is completed.
 */
export function tripEarningsPiasters(params: {
  distanceKm: number;
  ratePerKmPiasters: number;
  monthlyCapKm: number;
  kmAlreadyThisMonth: number;
}): number {
  const remainingCap = Math.max(0, params.monthlyCapKm - params.kmAlreadyThisMonth);
  const payableKm = Math.min(params.distanceKm, remainingCap);
  return Math.round(payableKm * params.ratePerKmPiasters);
}

/** Advertiser cost per driver-km: driver rate + Laseq take rate. */
export const PLATFORM_TAKE_RATE = 0.3;

export function advertiserCostPerKmPiasters(ratePerKmPiasters: number): number {
  return Math.round(ratePerKmPiasters / (1 - PLATFORM_TAKE_RATE));
}

export function monthStart(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
}
