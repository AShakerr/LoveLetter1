import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp } from "@/lib/money";
import { cityName, parseCities } from "@/lib/cities";
import { enumLabel } from "@/lib/i18n";
import { PageTitle, Empty, formatDate } from "@/components/ui";
import { driverNav } from "../_nav";

export default async function BrowseCampaigns() {
  const user = await requireRole("DRIVER");
  const { locale, t } = await getI18n();
  const campaigns = await db.campaign.findMany({
    where: { status: "ACTIVE", endDate: { gte: new Date() } },
    include: {
      advertiser: true,
      _count: { select: { applications: { where: { status: { in: ["ACCEPTED", "INSTALL_SUBMITTED", "ACTIVE"] } } } } },
      applications: { where: { driverId: user.driver!.id }, select: { status: true } },
    },
    orderBy: { createdAt: "desc" },
  });
  return (
    <Shell current="/driver/campaigns" nav={driverNav(t)}>
      <PageTitle title={t.browseCampaigns} />
      {campaigns.length === 0 && <Empty text={t.empty} />}
      <div className="grid gap-4 md:grid-cols-2">
        {campaigns.map((c) => {
          const mine = c.applications[0];
          const left = Math.max(0, c.driverSlots - c._count.applications);
          return (
            <Link key={c.id} href={`/driver/campaigns/${c.id}`} className="card block hover:border-nile-600">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-lg font-bold">{c.name}</div>
                  <div className="text-sm text-ink/60">{c.advertiser.companyName}</div>
                </div>
                {mine && <span className="badge bg-blue-100 text-blue-800">{enumLabel(t, "st", mine.status)}</span>}
              </div>
              <div className="mt-3 flex flex-wrap gap-1">
                {parseCities(c.cities).map((s) => (
                  <span key={s} className="badge bg-sand-100">
                    {cityName(s, locale)}
                  </span>
                ))}
                <span className="badge bg-sand-100">{enumLabel(t, "pl", c.placement)}</span>
              </div>
              <div className="mt-3 text-sm">
                <b className="text-nile-800">{formatEgp(c.ratePerKmPiasters, locale)}</b> {t.perKm}
                {c.monthlyBasePiasters > 0 && (
                  <>
                    {" + "}
                    <b className="text-nile-800">{formatEgp(c.monthlyBasePiasters, locale)}</b> {t.perMonth}
                  </>
                )}
              </div>
              <div className="mt-1 text-xs text-ink/60">
                {left} {t.slotsLeft} · {formatDate(c.startDate, locale)} → {formatDate(c.endDate, locale)}
              </div>
            </Link>
          );
        })}
      </div>
    </Shell>
  );
}
