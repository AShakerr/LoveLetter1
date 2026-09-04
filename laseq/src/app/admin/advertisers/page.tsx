import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { displayPhone } from "@/lib/phone";
import { reviewAdvertiserAction } from "@/app/actions/admin";
import { PageTitle, StatusBadge, Table, Empty } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminAdvertisers() {
  await requireRole("ADMIN");
  const { t } = await getI18n();
  const rows = await db.advertiser.findMany({ include: { user: true, _count: { select: { campaigns: true } } }, orderBy: [{ status: "asc" }, { createdAt: "desc" }] });
  return (
    <Shell current="/admin/advertisers" nav={adminNav(t)}>
      <PageTitle title={t.adminAdvertisers} />
      {rows.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <Table head={[t.companyName, t.contactName, t.phone, t.industry, t.taxId, t.campaigns, t.status, t.actions]}>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="px-4 py-3 font-semibold">{a.companyName}</td>
              <td className="px-4 py-3">{a.user.name}</td>
              <td className="px-4 py-3" dir="ltr">{displayPhone(a.user.phone)}</td>
              <td className="px-4 py-3">{a.industry ?? "—"}</td>
              <td className="px-4 py-3" dir="ltr">{a.taxId ?? "—"}</td>
              <td className="px-4 py-3">{a._count.campaigns}</td>
              <td className="px-4 py-3"><StatusBadge t={t} status={a.status} /></td>
              <td className="px-4 py-3">
                {a.status === "PENDING" && (
                  <form action={reviewAdvertiserAction} className="flex gap-2">
                    <input type="hidden" name="id" value={a.id} />
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
