import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp, formatNumber } from "@/lib/money";
import { PageTitle, StatCard } from "@/components/ui";
import { adminNav } from "./_nav";

export default async function AdminHome() {
  await requireRole("ADMIN");
  const { locale, t } = await getI18n();
  const [drivers, advertisers, campaigns, installs, payouts, flagged, totals] = await Promise.all([
    db.driverProfile.count({ where: { status: "PENDING" } }),
    db.advertiser.count({ where: { status: "PENDING" } }),
    db.campaign.count({ where: { status: "PENDING_REVIEW" } }),
    db.application.count({ where: { status: "INSTALL_SUBMITTED" } }),
    db.payout.count({ where: { status: "REQUESTED" } }),
    db.trip.count({ where: { status: "FLAGGED" } }),
    db.trip.aggregate({ where: { status: "COMPLETED" }, _sum: { distanceKm: true, estImpressions: true, earningsPiasters: true } }),
  ]);
  const queue = [
    { href: "/admin/drivers", label: t.adminDrivers, n: drivers },
    { href: "/admin/advertisers", label: t.adminAdvertisers, n: advertisers },
    { href: "/admin/campaigns", label: t.adminCampaigns, n: campaigns },
    { href: "/admin/installs", label: t.adminInstalls, n: installs },
    { href: "/admin/payouts", label: t.adminPayouts, n: payouts },
    { href: "/admin/trips", label: t.adminTrips, n: flagged },
  ];
  return (
    <Shell current="/admin" nav={adminNav(t)}>
      <PageTitle title={t.adminHome} />
      <div className="grid gap-4 md:grid-cols-3">
        <StatCard label={t.totalKm} value={formatNumber(totals._sum.distanceKm ?? 0, locale)} />
        <StatCard label={t.totalImpressions} value={formatNumber(totals._sum.estImpressions ?? 0, locale)} />
        <StatCard label={t.totalEarned} value={formatEgp(totals._sum.earningsPiasters ?? 0, locale)} />
      </div>
      <h2 className="mt-10 text-xl font-bold">{t.pendingReview}</h2>
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        {queue.map((q) => (
          <Link key={q.href} href={q.href} className="card flex items-center justify-between hover:border-nile-600">
            <span className="font-semibold">{q.label}</span>
            <span className={`badge ${q.n > 0 ? "bg-amber-100 text-amber-800" : "bg-gray-100 text-gray-600"}`}>{q.n}</span>
          </Link>
        ))}
      </div>
    </Shell>
  );
}
