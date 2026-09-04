import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { driverStats } from "@/lib/trips";
import { formatEgp, formatNumber } from "@/lib/money";
import { PageTitle, StatCard, StatusBadge, Empty } from "@/components/ui";
import { driverNav } from "./_nav";

export default async function DriverHome() {
  const user = await requireRole("DRIVER");
  const driver = user.driver!;
  const { locale, t } = await getI18n();
  const [stats, active] = await Promise.all([
    driverStats(driver.id),
    db.application.findMany({
      where: { driverId: driver.id, status: "ACTIVE", campaign: { status: "ACTIVE" } },
      include: { campaign: { include: { advertiser: true } } },
    }),
  ]);
  return (
    <Shell current="/driver" nav={driverNav(t)}>
      <PageTitle
        title={`${t.driverHome}${user.name ? ` · ${user.name}` : ""}`}
        action={<StatusBadge t={t} status={driver.status} />}
      />
      {driver.status === "PENDING" && <p className="mb-6 rounded-xl bg-amber-100 p-4 text-amber-900">{t.reviewPending}</p>}
      {driver.status === "REJECTED" && (
        <p className="mb-6 rounded-xl bg-red-100 p-4 text-red-900">
          {t.reviewRejected} {driver.reviewNote}
        </p>
      )}
      <div className="grid gap-4 md:grid-cols-4">
        <StatCard label={t.balance} value={formatEgp(stats.balance, locale)} />
        <StatCard label={t.totalEarned} value={formatEgp(stats.totalEarned, locale)} />
        <StatCard label={t.totalKm} value={`${formatNumber(stats.totalKm, locale)} ${t.km}`} />
        <StatCard label={t.totalImpressions} value={formatNumber(stats.totalImpressions, locale)} />
      </div>

      <h2 className="mt-10 text-xl font-bold">{t.activeCampaigns}</h2>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {active.length === 0 && <Empty text={t.empty} />}
        {active.map((a) => (
          <div key={a.id} className="card flex items-center justify-between gap-4">
            <div>
              <div className="font-bold">{a.campaign.name}</div>
              <div className="text-sm text-ink/60">{a.campaign.advertiser.companyName}</div>
              <div className="mt-1 text-sm">
                {t.youEarn} <b>{formatEgp(a.campaign.ratePerKmPiasters, locale)}</b> {t.perKm}
              </div>
            </div>
            <Link href={`/driver/trips/new?application=${a.id}`} className="btn-accent">
              {t.startTrip}
            </Link>
          </div>
        ))}
      </div>
      <div className="mt-6 flex gap-3">
        <Link href="/driver/campaigns" className="btn-primary">
          {t.browseCampaigns}
        </Link>
      </div>
    </Shell>
  );
}
