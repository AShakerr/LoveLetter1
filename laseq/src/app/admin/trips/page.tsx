import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatNumber } from "@/lib/money";
import { resolveTripAction } from "@/app/actions/admin";
import { PageTitle, Table, Empty, formatDateTime } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminTrips() {
  await requireRole("ADMIN");
  const { locale, t } = await getI18n();
  const rows = await db.trip.findMany({
    where: { status: "FLAGGED" },
    include: { application: { include: { campaign: true, driver: { include: { user: true } } } }, _count: { select: { pings: true } } },
    orderBy: { startedAt: "asc" },
  });
  return (
    <Shell current="/admin/trips" nav={adminNav(t)}>
      <PageTitle title={t.adminTrips} />
      {rows.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <Table head={[t.date, t.name, t.campaigns, t.km, t.impressions, t.pingsSent, t.flagged, t.actions]}>
          {rows.map((tr) => (
            <tr key={tr.id}>
              <td className="px-4 py-3">{formatDateTime(tr.startedAt, locale)}</td>
              <td className="px-4 py-3 font-semibold">{tr.application.driver.user.name}</td>
              <td className="px-4 py-3">{tr.application.campaign.name}</td>
              <td className="px-4 py-3">{tr.distanceKm.toFixed(1)}</td>
              <td className="px-4 py-3">{formatNumber(tr.estImpressions, locale)}</td>
              <td className="px-4 py-3">{tr._count.pings}</td>
              <td className="px-4 py-3 text-xs text-red-700" dir="ltr">{tr.flagReason}</td>
              <td className="px-4 py-3">
                <form action={resolveTripAction} className="flex gap-2">
                  <input type="hidden" name="id" value={tr.id} />
                  <button name="decision" value="approve" className="btn-primary px-3 py-1 text-xs">{t.clearFlag}</button>
                  <button name="decision" value="void" className="btn-danger px-3 py-1 text-xs">{t.voidTrip}</button>
                </form>
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Shell>
  );
}
