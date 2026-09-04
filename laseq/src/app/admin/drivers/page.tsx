import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { cityName } from "@/lib/cities";
import { enumLabel } from "@/lib/i18n";
import { displayPhone } from "@/lib/phone";
import { reviewDriverAction } from "@/app/actions/admin";
import { PageTitle, StatusBadge, Table, Empty } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminDrivers() {
  await requireRole("ADMIN");
  const { locale, t } = await getI18n();
  const drivers = await db.driverProfile.findMany({
    include: { user: true, vehicles: true },
    orderBy: [{ status: "asc" }, { createdAt: "desc" }],
    take: 200,
  });
  return (
    <Shell current="/admin/drivers" nav={adminNav(t)}>
      <PageTitle title={t.adminDrivers} />
      {drivers.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <Table head={[t.name, t.phone, t.city, t.vehicleSection, t.rideHailing, t.nationalId, t.status, t.actions]}>
          {drivers.map((d) => (
            <tr key={d.id}>
              <td className="px-4 py-3 font-semibold">{d.user.name}</td>
              <td className="px-4 py-3" dir="ltr">{displayPhone(d.user.phone)}</td>
              <td className="px-4 py-3">{cityName(d.city, locale)}</td>
              <td className="px-4 py-3">{d.vehicles.map((v) => `${v.make} ${v.model} ${v.year} (${v.plate})`).join(", ")}</td>
              <td className="px-4 py-3">{enumLabel(t, "rh", d.rideHailing)}</td>
              <td className="px-4 py-3" dir="ltr">{d.nationalId ?? "—"}</td>
              <td className="px-4 py-3"><StatusBadge t={t} status={d.status} /></td>
              <td className="px-4 py-3">
                {d.status === "PENDING" && (
                  <form action={reviewDriverAction} className="flex items-center gap-2">
                    <input type="hidden" name="id" value={d.id} />
                    <input name="note" placeholder={t.note} className="input w-32 py-1 text-xs" />
                    <button name="decision" value="approve" className="btn-primary px-3 py-1 text-xs">{t.approve}</button>
                    <button name="decision" value="reject" className="btn-danger px-3 py-1 text-xs">{t.reject}</button>
                  </form>
                )}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Shell>
  );
}
