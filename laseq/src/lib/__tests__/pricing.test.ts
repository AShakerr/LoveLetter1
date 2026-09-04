import { test } from "node:test";
import assert from "node:assert/strict";
import { tripEarningsPiasters, advertiserCostPerKmPiasters } from "../earnings";
import { estimateImpressions, hourMultiplier, cpmPiasters, estimateMonthlyCampaignImpressions } from "../impressions";
import { formatEgp, egpToPiasters } from "../money";

test("earnings respect the monthly km cap", () => {
  const rate = egpToPiasters(1.5);
  assert.equal(tripEarningsPiasters({ distanceKm: 40, ratePerKmPiasters: rate, monthlyCapKm: 1500, kmAlreadyThisMonth: 0 }), 6000);
  assert.equal(tripEarningsPiasters({ distanceKm: 40, ratePerKmPiasters: rate, monthlyCapKm: 1500, kmAlreadyThisMonth: 1480 }), 3000);
  assert.equal(tripEarningsPiasters({ distanceKm: 40, ratePerKmPiasters: rate, monthlyCapKm: 1500, kmAlreadyThisMonth: 1500 }), 0);
});

test("advertiser cost includes 30% take rate", () => {
  assert.equal(advertiserCostPerKmPiasters(150), 214);
});

test("impressions scale with city, hour and placement", () => {
  const rush = new Date("2026-09-01T15:30:00+03:00"); // Cairo is UTC+3 in summer (DST)
  const night = new Date("2026-09-01T02:00:00+03:00");
  const cairoRush = estimateImpressions({ distanceKm: 10, citySlug: "cairo", at: rush, placement: "REAR_WINDOW" });
  const cairoNight = estimateImpressions({ distanceKm: 10, citySlug: "cairo", at: night, placement: "REAR_WINDOW" });
  const luxorRush = estimateImpressions({ distanceKm: 10, citySlug: "luxor", at: rush, placement: "REAR_WINDOW" });
  const wrap = estimateImpressions({ distanceKm: 10, citySlug: "cairo", at: rush, placement: "FULL_WRAP" });
  assert.ok(cairoRush > cairoNight);
  assert.ok(cairoRush > luxorRush);
  assert.ok(wrap > cairoRush);
  assert.equal(hourMultiplier(3), 0.4);
});

test("monthly estimate and CPM are sane for a 20-car Cairo campaign", () => {
  const imp = estimateMonthlyCampaignImpressions({ driverSlots: 20, avgWeeklyKm: 350, monthlyCapKm: 1500, citySlugs: ["cairo", "giza"], placement: "REAR_WINDOW" });
  assert.ok(imp > 20_000_000 && imp < 40_000_000, `impressions ${imp}`);
  const spend = 20 * 1500 * advertiserCostPerKmPiasters(150);
  const cpm = cpmPiasters(spend, imp);
  assert.ok(cpm > 100 && cpm < 500, `cpm piasters ${cpm}`); // EGP 1–5 CPM
});

test("formatEgp", () => {
  assert.equal(formatEgp(150, "en"), "EGP 1.5");
  assert.equal(formatEgp(250000, "en"), "EGP 2,500");
});
