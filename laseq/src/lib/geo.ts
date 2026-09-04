export interface Ping {
  lat: number;
  lng: number;
  recordedAt: Date;
  speedKmh?: number | null;
}

const EARTH_RADIUS_KM = 6371;

export function haversineKm(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h));
}

export interface SegmentResult {
  distanceKm: number;
  /** Highest point-to-point speed seen, km/h */
  maxSegmentSpeedKmh: number;
  /** km discarded because the segment looked like GPS noise or teleporting */
  discardedKm: number;
}

/** Physically implausible for a car on Egyptian roads; treat as GPS jump. */
export const MAX_PLAUSIBLE_SPEED_KMH = 180;
/** Ignore jitter smaller than this when the car is basically parked. */
export const MIN_SEGMENT_KM = 0.01;

/**
 * Sum a ping trail into distance. Segments faster than MAX_PLAUSIBLE_SPEED_KMH
 * are discarded rather than counted, so a spoofed "teleport" cannot earn money.
 */
export function summarizePings(pings: Ping[]): SegmentResult {
  const sorted = [...pings].sort((a, b) => a.recordedAt.getTime() - b.recordedAt.getTime());
  let distanceKm = 0;
  let discardedKm = 0;
  let maxSegmentSpeedKmh = 0;
  for (let i = 1; i < sorted.length; i++) {
    const prev = sorted[i - 1];
    const cur = sorted[i];
    const km = haversineKm(prev, cur);
    if (km < MIN_SEGMENT_KM) continue;
    const hours = (cur.recordedAt.getTime() - prev.recordedAt.getTime()) / 3_600_000;
    const speed = hours > 0 ? km / hours : Number.POSITIVE_INFINITY;
    if (speed > MAX_PLAUSIBLE_SPEED_KMH) {
      discardedKm += km;
      continue;
    }
    maxSegmentSpeedKmh = Math.max(maxSegmentSpeedKmh, speed);
    distanceKm += km;
  }
  return { distanceKm, maxSegmentSpeedKmh, discardedKm };
}

export interface FraudVerdict {
  flagged: boolean;
  reasons: string[];
}

/**
 * Simple rule-based fraud screen run when a trip is closed.
 * Real deployment should add device attestation and accelerometer checks.
 */
export function screenTrip(params: {
  summary: SegmentResult;
  durationHours: number;
  pingCount: number;
  outsideCityRatio: number;
}): FraudVerdict {
  const reasons: string[] = [];
  const { summary, durationHours, pingCount, outsideCityRatio } = params;

  if (summary.distanceKm > 600) reasons.push("TRIP_TOO_LONG");
  if (durationHours > 0 && summary.distanceKm / durationHours > 120) reasons.push("AVG_SPEED_TOO_HIGH");
  if (summary.discardedKm > Math.max(2, summary.distanceKm * 0.2)) reasons.push("GPS_JUMPS");
  if (summary.distanceKm > 5 && pingCount / Math.max(summary.distanceKm, 1) < 0.5) reasons.push("TOO_FEW_PINGS");
  if (outsideCityRatio > 0.5) reasons.push("OUTSIDE_CAMPAIGN_CITIES");

  return { flagged: reasons.length > 0, reasons };
}
