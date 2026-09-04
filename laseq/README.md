# Laseq (لاصق) — car advertising marketplace for Egypt

Laseq is a two-sided marketplace modelled on Stic / Carvertise, built for the Egyptian
market: brands rent ad space on everyday cars and pay per km driven; drivers (especially
Uber / Careem / inDrive drivers) earn EGP for the km they already drive and cash out to
Vodafone Cash or InstaPay.

The whole product is Arabic-first (RTL) with a full English translation.

## What is in this MVP

| Area | Features |
| --- | --- |
| Public | Bilingual landing page (AR default, EN toggle), FAQ, launch cities |
| Auth | Egyptian mobile number + one-time code (OTP). No passwords. Signed session cookie |
| Driver | Onboarding (car, city, ride-hailing app, payout wallet, ID/licence), browse & apply to campaigns with eligibility rules, install-photo upload, GPS trip tracker (PWA-style web page), trip history, earnings & payout requests |
| Advertiser | Company onboarding, campaign builder with live reach/cost/CPM estimate, applicant review, per-campaign dashboard (active cars, km, impressions, spend, CPM), pause/resume |
| Admin | Queues for driver KYC, advertiser verification, campaign approval, install-photo verification, payouts, flagged trips |
| Engine | Haversine distance from GPS pings, city geofences (only km inside campaign cities are paid), impressions model by city × hour × placement, monthly km cap, 30% platform take rate, rule-based fraud screen (GPS teleports, absurd speeds, too few pings, off-city driving) |
| API | `POST /api/trips`, `POST /api/trips/:id/pings`, `POST /api/trips/:id/end` — the contract a native mobile app would use |

## Run it

```bash
cd laseq
cp .env.example .env          # already done if you cloned fresh; edit AUTH_SECRET
npm install
npm run db:generate           # generate Prisma client
npm run db:reset              # create SQLite db + seed demo data
npm run dev                   # http://localhost:3000
```

OTP codes are printed to the terminal running the server (`OTP_PROVIDER=console`).

### Demo accounts (after `npm run db:reset`)

| Role | Phone | Notes |
| --- | --- | --- |
| Admin | `01000000000` | `ADMIN_PHONE` in `.env`; any phone set there becomes admin on login |
| Driver | `01011111111` | Approved, active on "Fresh Cairo", has trips + balance |
| Driver | `01022222222` | Pending KYC — approve from `/admin/drivers` |
| Advertiser | `01033333333` | Juhayna, approved, two campaigns |

Any other Egyptian number creates a fresh account and goes through onboarding.

### Checks

```bash
npm run typecheck
npm run lint
npm test          # domain-logic unit tests (phone, geo, pricing)
npm run build
```

## Architecture

- **Next.js 16 (App Router) + React 19 + TypeScript + Tailwind v4**. Server components render
  everything; forms use server actions; the trip tracker is a client component.
- **Prisma 7 + SQLite** for local dev via the better-sqlite3 adapter. The schema is
  Postgres-ready: switch `datasource.provider` and the adapter for production.
- **Money is integer piasters** (1 EGP = 100). Never floats.
- **i18n** is a typed dictionary in `src/lib/i18n.ts` with a locale cookie; `<html dir>` flips per locale.

```
src/
  app/
    page.tsx                  landing
    login/, onboarding/       phone OTP, role choice, driver & advertiser forms
    driver/                   dashboard, campaigns, applications, trips, tracker, earnings
    advertiser/               dashboard, campaign builder, campaign detail
    admin/                    review queues
    actions/                  server actions (auth, onboarding, driver, advertiser, admin)
    api/trips/                trip lifecycle API
  lib/
    cities.ts                 launch cities, geofences, impressions/km baseline
    impressions.ts            city × hour × placement impressions model, CPM
    earnings.ts               per-km pay with monthly cap, platform take rate
    geo.ts                    haversine, ping summarisation, fraud screen
    trips.ts                  start / ping / end trip, stats
    eligibility.ts            can this driver+car join this campaign?
    auth.ts, session.ts, otp.ts, phone.ts, i18n.ts, money.ts, db.ts
prisma/schema.prisma          data model
prisma/seed.ts                demo data
```

## How the numbers work

- **Driver pay**: `ratePerKm × paidKm`, where paidKm counts only segments inside the
  campaign's cities and stops at `monthlyCapKm`. Campaigns can add a monthly base.
- **Advertiser cost per km** = driver rate ÷ (1 − 30% take rate).
- **Impressions** = km × city baseline (Cairo 950/km … Aswan 300/km) × hour multiplier
  (rush hour 1.4–1.5×, night 0.4×) × placement (rear window 1×, doors 1.35×, full wrap 1.9×).
  These are heuristics to be calibrated against real campaign data (QR scans, brand-lift surveys).
- **Fraud**: segments faster than 180 km/h are discarded; trips are flagged (zero pay until
  an admin clears them) for average speed > 120 km/h, > 600 km, heavy GPS jumping, too few
  pings per km, or mostly-outside-city driving.

## Egypt-specific notes

- **Phone numbers** accept 010/011/012/015 in local, +20, 0020 and Arabic-Indic digit forms.
- **Payouts** target Vodafone Cash, InstaPay and bank transfer. Integrate Paymob / Fawry /
  bank APIs behind `markPayoutPaidAction` for automatic disbursement.
- **OTP delivery**: `OTP_PROVIDER=console` for dev. WhatsApp Business API has the best
  reach in Egypt; SMS via a local aggregator as fallback. Implement in `src/lib/otp.ts`.
- **Regulation**: advertising on vehicles is licensed by the Traffic Department (إدارة المرور)
  and may require per-vehicle approval and stamped creative; verify current rules and
  budget for licensing per campaign before launch. Ride-hailing drivers should also check
  their platform's policy on exterior branding.
- **Install network**: the model assumes partner sticker/wrap shops (e.g. in Nasr City,
  Mohandessin, 6th of October, Smouha). Install verification is photo-based in this MVP.

## Roadmap (not in this MVP)

1. Native driver app (Expo) with background location, offline ping buffering, device attestation.
2. Postgres + PostGIS for real polygon geofences and route heatmaps for advertisers.
3. Automated payouts (Paymob/Fawry), advertiser invoicing with VAT (14%).
4. Object storage (S3-compatible) for creatives and install photos instead of `public/uploads`.
5. Periodic re-verification selfies of the sticker, and a QR/landing-page per campaign for attribution.
6. Rate limiting and audit log on admin actions.
