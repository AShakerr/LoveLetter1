import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp, formatNumber } from "@/lib/money";
import { PageTitle, StatusBadge, Empty, Table, formatDateTime } from "@/components/ui";
import { driverNav } from "../_nav";

export default async function Trips() {
  const user = await requireRole("DRIVER");
  const { locale, t } = await getI18n();
  const trips = await db.trip.findMany({
    where: { application: { driverId: user.driver!.id } },
    include: { application: { include: { campaign: true } } },
    orderBy: { startedAt: "desc" },
    take: 100,
  });
  return (
    <Shell current="/driver/trips" nav={driverNav(t)}>
      <PageTitle
        title={t.trips}
        action={
          <Link href="/driver/trips/new" className="btn-accent">
            {t.startTrip}
          </Link>
        }
      />
      {trips.length === 0 ? (
        <Empty text={t.noTrips} />
      ) : (
        <Table head={[t.date, t.campaigns, t.km, t.impressions, t.earnings, t.status]}>
          {trips.map((tr) => (
            <tr key={tr.id}>
              <td className="px-4 py-3">{formatDateTime(tr.startedAt, locale)}</td>
              <td className="px-4 py-3">{tr.application.campaign.name}</td>
              <td className="px-4 py-3">{tr.distanceKm.toFixed(1)}</td>
              <td className="px-4 py-3">{formatNumber(tr.estImpressions, locale)}</td>
              <td className="px-4 py-3 font-semibold">{formatEgp(tr.earningsPiasters, locale)}</td>
              <td className="px-4 py-3">
                <StatusBadge t={t} status={tr.status} />
                {tr.flagReason && <div className="text-xs text-red-700" dir="ltr">{tr.flagReason}</div>}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Shell>
  );
}
