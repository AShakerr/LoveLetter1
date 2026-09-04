/**
 * Demo data for local development.
 * Logins (OTP is printed to the server log):
 *   Admin      +201000000000  (ADMIN_PHONE in .env)
 *   Driver     +201011111111  (approved, active on "Fresh Cairo" campaign, has trips)
 *   Driver     +201022222222  (pending review)
 *   Advertiser +201033333333  (approved, owns 2 campaigns)
 */
import "dotenv/config";
import { PrismaClient } from "../src/generated/prisma/client";
import { PrismaBetterSqlite3 } from "@prisma/adapter-better-sqlite3";

const db = new PrismaClient({ adapter: new PrismaBetterSqlite3({ url: process.env.DATABASE_URL ?? "file:./dev.db" }) });

const EGP = (n: number) => Math.round(n * 100);
const days = (n: number) => new Date(Date.now() + n * 86_400_000);

async function main() {
  await db.locationPing.deleteMany();
  await db.trip.deleteMany();
  await db.payout.deleteMany();
  await db.application.deleteMany();
  await db.campaign.deleteMany();
  await db.vehicle.deleteMany();
  await db.driverProfile.deleteMany();
  await db.advertiser.deleteMany();
  await db.otpCode.deleteMany();
  await db.user.deleteMany();

  await db.user.create({ data: { phone: "+201000000000", name: "Laseq Admin", role: "ADMIN", locale: "en" } });

  const adv = await db.user.create({
    data: {
      phone: "+201033333333",
      name: "Sara Mahmoud",
      role: "ADVERTISER",
      advertiser: { create: { companyName: "Juhayna", industry: "FMCG", website: "https://juhayna.com", status: "APPROVED" } },
    },
    include: { advertiser: true },
  });

  const freshCairo = await db.campaign.create({
    data: {
      advertiserId: adv.advertiser!.id,
      name: "Fresh Cairo — Juhayna Juice",
      description:
        "Rear-window sticker for Juhayna's new mango juice. Drive normally in Cairo & Giza. Sticker installed free at our partner shops in Nasr City, Mohandessin and 6th of October.\n\nملصق على الزجاج الخلفي لعصير مانجو جهينة الجديد. سوق طبيعي في القاهرة والجيزة.",
      creativeUrl: "https://images.unsplash.com/photo-1600271886742-f049cd451bba?w=800",
      placement: "REAR_WINDOW",
      cities: JSON.stringify(["cairo", "giza"]),
      allowedBodyTypes: "[]",
      minCarYear: 2012,
      budgetPiasters: EGP(250_000),
      ratePerKmPiasters: EGP(1.5),
      monthlyBasePiasters: EGP(500),
      monthlyCapKm: 1500,
      driverSlots: 50,
      startDate: days(-20),
      endDate: days(70),
      status: "ACTIVE",
    },
  });

  await db.campaign.create({
    data: {
      advertiserId: adv.advertiser!.id,
      name: "Alex Summer — Side doors",
      description: "Side-door branding for the Alexandria summer season. Corniche routes preferred.",
      placement: "SIDE_DOORS",
      cities: JSON.stringify(["alexandria"]),
      allowedBodyTypes: JSON.stringify(["SEDAN", "SUV"]),
      minCarYear: 2015,
      budgetPiasters: EGP(120_000),
      ratePerKmPiasters: EGP(2.25),
      monthlyBasePiasters: EGP(800),
      monthlyCapKm: 1200,
      driverSlots: 20,
      startDate: days(10),
      endDate: days(100),
      status: "PENDING_REVIEW",
    },
  });

  const driver1 = await db.user.create({
    data: {
      phone: "+201011111111",
      name: "Ahmed Hassan",
      role: "DRIVER",
      driver: {
        create: {
          city: "cairo",
          nationalId: "29901011234567",
          licenseNumber: "CAI-4471120",
          rideHailing: "UBER",
          avgWeeklyKm: 900,
          payoutMethod: "VODAFONE_CASH",
          payoutAccount: "01011111111",
          status: "APPROVED",
          vehicles: { create: { make: "Hyundai", model: "Elantra", year: 2019, color: "White", plate: "س ط ر 4821", bodyType: "SEDAN" } },
        },
      },
    },
    include: { driver: { include: { vehicles: true } } },
  });

  await db.user.create({
    data: {
      phone: "+201022222222",
      name: "Mona Adel",
      role: "DRIVER",
      driver: {
        create: {
          city: "giza",
          rideHailing: "CAREEM",
          avgWeeklyKm: 400,
          payoutMethod: "INSTAPAY",
          payoutAccount: "mona@instapay",
          status: "PENDING",
          vehicles: { create: { make: "Nissan", model: "Sunny", year: 2017, color: "Silver", plate: "ق و ن 1190", bodyType: "SEDAN" } },
        },
      },
    },
  });

  const app = await db.application.create({
    data: {
      campaignId: freshCairo.id,
      driverId: driver1.driver!.id,
      vehicleId: driver1.driver!.vehicles[0].id,
      status: "ACTIVE",
      installVerifiedAt: days(-15),
      appliedAt: days(-18),
    },
  });

  // A few completed trips along real Cairo corridors (Ring Road / Nasr City / Maadi).
  const trips = [
    { day: -12, km: 38.4, impressions: 41_900 },
    { day: -10, km: 52.1, impressions: 60_300 },
    { day: -7, km: 27.8, impressions: 24_100 },
    { day: -4, km: 61.0, impressions: 77_800 },
    { day: -1, km: 44.6, impressions: 49_200 },
  ];
  for (const tr of trips) {
    const started = days(tr.day);
    await db.trip.create({
      data: {
        applicationId: app.id,
        startedAt: started,
        endedAt: new Date(started.getTime() + 3 * 3_600_000),
        distanceKm: tr.km,
        estImpressions: tr.impressions,
        earningsPiasters: Math.round(tr.km * freshCairo.ratePerKmPiasters),
        status: "COMPLETED",
      },
    });
  }
  await db.trip.create({
    data: {
      applicationId: app.id,
      startedAt: days(-2),
      endedAt: new Date(days(-2).getTime() + 40 * 60_000),
      distanceKm: 210.5,
      estImpressions: 0,
      earningsPiasters: 0,
      status: "FLAGGED",
      flagReason: "AVG_SPEED_TOO_HIGH,GPS_JUMPS",
    },
  });

  await db.payout.create({
    data: { driverId: driver1.driver!.id, amountPiasters: EGP(120), method: "VODAFONE_CASH", account: "01011111111", status: "PAID", paidAt: days(-5), reference: "VFC-88213" },
  });

  console.log("Seeded: 1 admin, 2 drivers, 1 advertiser, 2 campaigns, 6 trips.");
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(() => db.$disconnect());
