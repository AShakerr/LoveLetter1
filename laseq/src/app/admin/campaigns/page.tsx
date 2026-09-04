import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp } from "@/lib/money";
import { cityName, parseCities } from "@/lib/cities";
import { reviewCampaignAction } from "@/app/actions/admin";
import { PageTitle, StatusBadge, Table, Empty, formatDate } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminCampaigns() {
  await requireRole("ADMIN");
  const { locale, t } = await getI18n();
  const rows = await db.campaign.findMany({ include: { advertiser: true }, orderBy: [{ createdAt: "desc" }], take: 200 });
  const order = { PENDING_REVIEW: 0, ACTIVE: 1, PAUSED: 2, DRAFT: 3, COMPLETED: 4, REJECTED: 5 } as const;
  rows.sort((a, b) => order[a.status] - order[b.status]);
  return (
    <Shell current="/admin/campaigns" nav={adminNav(t)}>
      <PageTitle title={t.adminCampaigns} />
      {rows.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <Table head={[t.campaignName, t.companyName, t.cities, t.driverSlots, t.ratePerKm, t.budget, t.runs, t.status, t.actions]}>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="px-4 py-3 font-semibold">{c.name}</td>
              <td className="px-4 py-3">{c.advertiser.companyName}</td>
              <td className="px-4 py-3">{parseCities(c.cities).map((s) => cityName(s, locale)).join("، ")}</td>
              <td className="px-4 py-3">{c.driverSlots}</td>
              <td className="px-4 py-3">{formatEgp(c.ratePerKmPiasters, locale)}</td>
              <td className="px-4 py-3">{formatEgp(c.budgetPiasters, locale)}</td>
              <td className="px-4 py-3 whitespace-nowrap">{formatDate(c.startDate, locale)} → {formatDate(c.endDate, locale)}</td>
              <td className="px-4 py-3"><StatusBadge t={t} status={c.status} /></td>
              <td className="px-4 py-3">
                {c.status === "PENDING_REVIEW" && (
                  <form action={reviewCampaignAction} className="flex items-center gap-2">
                    <input type="hidden" name="id" value={c.id} />
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
