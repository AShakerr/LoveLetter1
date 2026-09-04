import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { formatEgp } from "@/lib/money";
import { enumLabel } from "@/lib/i18n";
import { displayPhone } from "@/lib/phone";
import { markPayoutPaidAction } from "@/app/actions/admin";
import { PageTitle, StatusBadge, Table, Empty, formatDate } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminPayouts() {
  await requireRole("ADMIN");
  const { locale, t } = await getI18n();
  const rows = await db.payout.findMany({
    include: { driver: { include: { user: true } } },
    orderBy: [{ status: "desc" }, { requestedAt: "asc" }],
    take: 200,
  });
  return (
    <Shell current="/admin/payouts" nav={adminNav(t)}>
      <PageTitle title={t.adminPayouts} />
      {rows.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <Table head={[t.date, t.name, t.phone, t.amount, t.payoutMethod, t.payoutAccount, t.status, t.actions]}>
          {rows.map((p) => (
            <tr key={p.id}>
              <td className="px-4 py-3">{formatDate(p.requestedAt, locale)}</td>
              <td className="px-4 py-3 font-semibold">{p.driver.user.name}</td>
              <td className="px-4 py-3" dir="ltr">{displayPhone(p.driver.user.phone)}</td>
              <td className="px-4 py-3 font-semibold">{formatEgp(p.amountPiasters, locale)}</td>
              <td className="px-4 py-3">{enumLabel(t, "pm", p.method)}</td>
              <td className="px-4 py-3" dir="ltr">{p.account}</td>
              <td className="px-4 py-3">
                <StatusBadge t={t} status={p.status} />
                {p.reference && <div className="text-xs text-ink/50" dir="ltr">{p.reference}</div>}
              </td>
              <td className="px-4 py-3">
                {p.status === "REQUESTED" && (
                  <form action={markPayoutPaidAction} className="flex items-center gap-2">
                    <input type="hidden" name="id" value={p.id} />
                    <input name="reference" placeholder={t.reference} className="input w-36 py-1 text-xs" dir="ltr" />
                    <button name="decision" value="paid" className="btn-primary px-3 py-1 text-xs">{t.markPaid}</button>
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
