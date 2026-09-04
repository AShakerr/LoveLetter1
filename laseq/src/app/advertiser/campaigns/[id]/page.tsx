import { notFound } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { campaignStats } from "@/lib/trips";
import { formatEgp, formatNumber } from "@/lib/money";
import { advertiserCostPerKmPiasters } from "@/lib/earnings";
import { cpmPiasters } from "@/lib/impressions";
import { cityName, parseCities } from "@/lib/cities";
import { enumLabel } from "@/lib/i18n";
import { decideApplicationAction, submitCampaignAction, toggleCampaignPauseAction } from "@/app/actions/advertiser";
import { PageTitle, StatCard, StatusBadge, Empty, Table, formatDate } from "@/components/ui";
import { advertiserNav } from "../../_nav";

export default async function CampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const user = await requireRole("ADVERTISER");
  const { locale, t } = await getI18n();
  const campaign = await db.campaign.findFirst({
    where: { id, advertiserId: user.advertiser!.id },
    include: {
      applications: {
        include: { driver: { include: { user: true } }, vehicle: true },
        orderBy: { appliedAt: "desc" },
      },
    },
  });
  if (!campaign) notFound();
  const stats = await campaignStats(campaign.id);
  const spend = Math.round(stats.km * advertiserCostPerKmPiasters(campaign.ratePerKmPiasters));
  const budgetPct = campaign.budgetPiasters > 0 ? Math.min(100, Math.round((spend / campaign.budgetPiasters) * 100)) : 0;

  return (
    <Shell current={`/advertiser/campaigns/${id}`} nav={advertiserNav(t)}>
      <PageTitle
        title={campaign.name}
        subtitle={`${parseCities(campaign.cities).map((s) => cityName(s, locale)).join("، ")} · ${enumLabel(t, "pl", campaign.placement)} · ${formatDate(campaign.startDate, locale)} → ${formatDate(campaign.endDate, locale)}`}
        action={
          <div className="flex items-center gap-2">
            <StatusBadge t={t} status={campaign.status} />
            {campaign.status === "DRAFT" && (
              <form action={submitCampaignAction}>
                <input type="hidden" name="campaignId" value={campaign.id} />
                <button className="btn-accent text-sm">{t.submitForReview}</button>
              </form>
            )}
            {(campaign.status === "ACTIVE" || campaign.status === "PAUSED") && (
              <form action={toggleCampaignPauseAction}>
                <input type="hidden" name="campaignId" value={campaign.id} />
                <button className="btn-ghost text-sm">{campaign.status === "ACTIVE" ? t.pause : t.resume}</button>
              </form>
            )}
          </div>
        }
      />
      {campaign.reviewNote && <p className="mb-6 rounded-xl bg-red-100 p-4 text-red-900">{campaign.reviewNote}</p>}
      <div className="grid gap-4 md:grid-cols-5">
        <StatCard label={t.activeCars} value={`${stats.activeCars} / ${campaign.driverSlots}`} />
        <StatCard label={t.kmDriven} value={formatNumber(stats.km, locale)} />
        <StatCard label={t.impressions} value={formatNumber(stats.impressions, locale)} />
        <StatCard label={t.spend} value={formatEgp(spend, locale)} hint={`${budgetPct}% ${t.budgetUsed} · ${formatEgp(campaign.budgetPiasters, locale)}`} />
        <StatCard label={t.cpm} value={formatEgp(cpmPiasters(spend, stats.impressions), locale)} />
      </div>

      <h2 className="mt-10 text-xl font-bold">{t.applicants}</h2>
      <div className="mt-4">
        {campaign.applications.length === 0 ? (
          <Empty text={t.noApplicants} />
        ) : (
          <Table head={[t.name, t.city, t.vehicleSection, t.rideHailing, t.date, t.status, t.actions]}>
            {campaign.applications.map((a) => (
              <tr key={a.id}>
                <td className="px-4 py-3 font-semibold">{a.driver.user.name}</td>
                <td className="px-4 py-3">{cityName(a.driver.city, locale)}</td>
                <td className="px-4 py-3">
                  {a.vehicle.make} {a.vehicle.model} {a.vehicle.year} · {enumLabel(t, "bt", a.vehicle.bodyType)}
                </td>
                <td className="px-4 py-3">{enumLabel(t, "rh", a.driver.rideHailing)}</td>
                <td className="px-4 py-3">{formatDate(a.appliedAt, locale)}</td>
                <td className="px-4 py-3"><StatusBadge t={t} status={a.status} /></td>
                <td className="px-4 py-3">
                  {a.status === "APPLIED" && (
                    <form action={decideApplicationAction} className="flex gap-2">
                      <input type="hidden" name="applicationId" value={a.id} />
                      <button name="decision" value="accept" className="btn-primary px-3 py-1 text-xs">{t.acceptDriver}</button>
                      <button name="decision" value="reject" className="btn-danger px-3 py-1 text-xs">{t.reject}</button>
                    </form>
                  )}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    </Shell>
  );
}
