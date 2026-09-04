import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { campaignStats } from "@/lib/trips";
import { formatEgp, formatNumber } from "@/lib/money";
import { advertiserCostPerKmPiasters } from "@/lib/earnings";
import { PageTitle, StatCard, StatusBadge, Empty, Table } from "@/components/ui";
import { advertiserNav } from "./_nav";

export default async function AdvertiserHome() {
  const user = await requireRole("ADVERTISER");
  const adv = user.advertiser!;
  const { locale, t } = await getI18n();
  const campaigns = await db.campaign.findMany({ where: { advertiserId: adv.id }, orderBy: { createdAt: "desc" } });
  const stats = await Promise.all(campaigns.map((c) => campaignStats(c.id)));
  const totals = stats.reduce(
    (acc, s, i) => {
      acc.km += s.km;
      acc.impressions += s.impressions;
      acc.spend += Math.round((s.km * advertiserCostPerKmPiasters(campaigns[i].ratePerKmPiasters)));
      acc.activeCars += s.activeCars;
      return acc;
    },
    { km: 0, impressions: 0, spend: 0, activeCars: 0 },
  );
  return (
    <Shell current="/advertiser" nav={advertiserNav(t)}>
      <PageTitle
        title={`${t.advertiserHome} · ${adv.companyName}`}
        action={
          <div className="flex items-center gap-2">
            <StatusBadge t={t} status={adv.status} />
            <Link href="/advertiser/campaigns/new" className="btn-accent">
              {t.newCampaign}
            </Link>
          </div>
        }
      />
      {adv.status === "PENDING" && <p className="mb-6 rounded-xl bg-amber-100 p-4 text-amber-900">{t.advertiserPending}</p>}
      <div className="grid gap-4 md:grid-cols-4">
        <StatCard label={t.activeCars} value={formatNumber(totals.activeCars, locale)} />
        <StatCard label={t.kmDriven} value={formatNumber(totals.km, locale)} />
        <StatCard label={t.impressions} value={formatNumber(totals.impressions, locale)} />
        <StatCard label={t.spend} value={formatEgp(totals.spend, locale)} />
      </div>
      <h2 className="mt-10 text-xl font-bold">{t.campaigns}</h2>
      <div className="mt-4">
        {campaigns.length === 0 ? (
          <Empty text={t.empty} />
        ) : (
          <Table head={[t.campaignName, t.status, t.activeCars, t.kmDriven, t.impressions, t.budget]}>
            {campaigns.map((c, i) => (
              <tr key={c.id}>
                <td className="px-4 py-3">
                  <Link href={`/advertiser/campaigns/${c.id}`} className="font-semibold hover:underline">
                    {c.name}
                  </Link>
                </td>
                <td className="px-4 py-3"><StatusBadge t={t} status={c.status} /></td>
                <td className="px-4 py-3">{stats[i].activeCars} / {c.driverSlots}</td>
                <td className="px-4 py-3">{formatNumber(stats[i].km, locale)}</td>
                <td className="px-4 py-3">{formatNumber(stats[i].impressions, locale)}</td>
                <td className="px-4 py-3">{formatEgp(c.budgetPiasters, locale)}</td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    </Shell>
  );
}
