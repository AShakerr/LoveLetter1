import { notFound } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp, formatNumber } from "@/lib/money";
import { cityName, parseCities } from "@/lib/cities";
import { enumLabel } from "@/lib/i18n";
import { checkEligibility, type EligibilityReason } from "@/lib/eligibility";
import { applyToCampaignAction } from "@/app/actions/driver";
import { PageTitle, StatusBadge, formatDate } from "@/components/ui";
import { driverNav } from "../../_nav";

const REASON_KEY: Record<EligibilityReason, "reasonProfileNotApproved" | "reasonCity" | "reasonYear" | "reasonBodyType" | "reasonFull"> = {
  PROFILE_NOT_APPROVED: "reasonProfileNotApproved",
  CITY: "reasonCity",
  YEAR: "reasonYear",
  BODY_TYPE: "reasonBodyType",
  FULL: "reasonFull",
};

export default async function CampaignDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const user = await requireRole("DRIVER");
  const driver = user.driver!;
  const { locale, t } = await getI18n();
  const campaign = await db.campaign.findFirst({
    where: { id, status: { in: ["ACTIVE", "PAUSED", "COMPLETED"] } },
    include: {
      advertiser: true,
      _count: { select: { applications: { where: { status: { in: ["ACCEPTED", "INSTALL_SUBMITTED", "ACTIVE"] } } } } },
    },
  });
  if (!campaign) notFound();
  const mine = await db.application.findUnique({ where: { campaignId_driverId: { campaignId: id, driverId: driver.id } } });
  const vehicle = driver.vehicles[0];
  const reasons = vehicle
    ? checkEligibility({ driver, vehicle, campaign, filledSlots: campaign._count.applications })
    : (["PROFILE_NOT_APPROVED"] as EligibilityReason[]);
  const allowedBodyTypes: string[] = JSON.parse(campaign.allowedBodyTypes || "[]");

  return (
    <Shell current={`/driver/campaigns/${id}`} nav={driverNav(t)}>
      <PageTitle title={campaign.name} subtitle={campaign.advertiser.companyName} action={mine ? <StatusBadge t={t} status={mine.status} /> : undefined} />
      <div className="grid gap-6 md:grid-cols-3">
        <div className="card md:col-span-2">
          {campaign.creativeUrl && (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={campaign.creativeUrl} alt="" className="mb-4 max-h-64 rounded-xl object-cover" />
          )}
          <p className="whitespace-pre-line text-ink/80">{campaign.description}</p>
          <dl className="mt-6 grid gap-3 text-sm md:grid-cols-2">
            <Row k={t.cities} v={parseCities(campaign.cities).map((s) => cityName(s, locale)).join("، ")} />
            <Row k={t.placement} v={enumLabel(t, "pl", campaign.placement)} />
            <Row k={t.runs} v={`${formatDate(campaign.startDate, locale)} → ${formatDate(campaign.endDate, locale)}`} />
            <Row k={t.minCarYear} v={String(campaign.minCarYear)} />
            <Row k={t.bodyType} v={allowedBodyTypes.length ? allowedBodyTypes.map((b) => enumLabel(t, "bt", b)).join("، ") : t.none} />
            <Row k={t.slotsLeft} v={String(Math.max(0, campaign.driverSlots - campaign._count.applications))} />
          </dl>
        </div>
        <div className="space-y-4">
          <div className="card">
            <div className="text-sm text-ink/60">{t.youEarn}</div>
            <div className="text-3xl font-extrabold text-nile-800">{formatEgp(campaign.ratePerKmPiasters, locale)}</div>
            <div className="text-sm text-ink/60">{t.perKm}</div>
            {campaign.monthlyBasePiasters > 0 && (
              <div className="mt-3 text-sm">
                {t.monthlyBase}: <b>{formatEgp(campaign.monthlyBasePiasters, locale)}</b>
              </div>
            )}
            <div className="mt-1 text-sm">
              {t.monthlyCap}: <b>{formatNumber(campaign.monthlyCapKm, locale)} {t.km}</b>
            </div>
          </div>
          {mine ? (
            <div className="card">
              <StatusBadge t={t} status={mine.status} />
              {mine.status === "ACCEPTED" && <p className="mt-2 text-sm">{t.accepted}</p>}
            </div>
          ) : reasons.length === 0 && campaign.status === "ACTIVE" ? (
            <form action={applyToCampaignAction}>
              <input type="hidden" name="campaignId" value={campaign.id} />
              <input type="hidden" name="vehicleId" value={vehicle?.id} />
              <button className="btn-accent w-full">{t.apply}</button>
            </form>
          ) : (
            <div className="card">
              <div className="font-bold">{t.notEligible}</div>
              <ul className="mt-2 list-disc ps-5 text-sm text-ink/70">
                {reasons.map((r) => (
                  <li key={r}>{t[REASON_KEY[r]]}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </Shell>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt className="text-ink/60">{k}</dt>
      <dd className="font-semibold">{v}</dd>
    </div>
  );
}
