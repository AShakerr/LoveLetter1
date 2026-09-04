import { test } from "node:test";
import assert from "node:assert/strict";
import { haversineKm, summarizePings, screenTrip } from "../geo";
import { isInsideCity } from "../cities";

const t0 = Date.parse("2026-09-01T08:00:00Z");
const at = (mins: number) => new Date(t0 + mins * 60_000);

test("haversine: Tahrir Square to Cairo Airport ≈ 17-18 km", () => {
  const km = haversineKm({ lat: 30.0444, lng: 31.2357 }, { lat: 30.1219, lng: 31.4056 });
  assert.ok(km > 17 && km < 19, `got ${km}`);
});

test("summarizePings sums plausible segments and drops teleports", () => {
  const pings = [
    { lat: 30.0444, lng: 31.2357, recordedAt: at(0) },
    { lat: 30.05, lng: 31.24, recordedAt: at(2) }, // ~0.74 km in 2 min
    { lat: 30.06, lng: 31.25, recordedAt: at(4) },
    { lat: 31.2, lng: 29.9, recordedAt: at(5) }, // Alexandria 1 min later: impossible
  ];
  const s = summarizePings(pings);
  assert.ok(s.distanceKm > 2.0 && s.distanceKm < 2.4, `distance ${s.distanceKm}`);
  assert.ok(s.discardedKm > 150, `discarded ${s.discardedKm}`);
});

test("screenTrip flags absurd trips and passes normal ones", () => {
  const normal = screenTrip({
    summary: { distanceKm: 35, maxSegmentSpeedKmh: 80, discardedKm: 0 },
    durationHours: 2,
    pingCount: 400,
    outsideCityRatio: 0.05,
  });
  assert.equal(normal.flagged, false);

  const bad = screenTrip({
    summary: { distanceKm: 210, maxSegmentSpeedKmh: 170, discardedKm: 90 },
    durationHours: 0.7,
    pingCount: 20,
    outsideCityRatio: 0.9,
  });
  assert.equal(bad.flagged, true);
  assert.ok(bad.reasons.includes("AVG_SPEED_TOO_HIGH"));
  assert.ok(bad.reasons.includes("GPS_JUMPS"));
  assert.ok(bad.reasons.includes("OUTSIDE_CAMPAIGN_CITIES"));
});

test("city geofences contain landmarks", () => {
  assert.ok(isInsideCity(30.0444, 31.2357, "cairo")); // Tahrir
  assert.ok(isInsideCity(29.9773, 31.1325, "giza")); // Pyramids
  assert.ok(isInsideCity(31.2001, 29.9187, "alexandria"));
  assert.ok(!isInsideCity(31.2001, 29.9187, "cairo"));
});
