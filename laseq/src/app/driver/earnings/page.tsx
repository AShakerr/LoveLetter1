import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { driverStats } from "@/lib/trips";
import { formatEgp } from "@/lib/money";
import { enumLabel } from "@/lib/i18n";
import { PageTitle, StatCard, StatusBadge, Table, Empty, formatDate } from "@/components/ui";
import { PayoutForm } from "./PayoutForm";
import { driverNav } from "../_nav";

export default async function Earnings() {
  const user = await requireRole("DRIVER");
  const driver = user.driver!;
  const { locale, t } = await getI18n();
  const [stats, payouts] = await Promise.all([
    driverStats(driver.id),
    db.payout.findMany({ where: { driverId: driver.id }, orderBy: { requestedAt: "desc" } }),
  ]);
  return (
    <Shell current="/driver/earnings" nav={driverNav(t)}>
      <PageTitle title={t.earnings} />
      <div className="grid gap-4 md:grid-cols-3">
        <StatCard label={t.balance} value={formatEgp(stats.balance, locale)} />
        <StatCard label={t.totalEarned} value={formatEgp(stats.totalEarned, locale)} />
        <StatCard label={t.payoutMethod} value={enumLabel(t, "pm", driver.payoutMethod)} hint={driver.payoutAccount ?? ""} />
      </div>
      <div className="mt-6">
        <PayoutForm
          balanceEgp={stats.balance / 100}
          labels={{ request: t.requestPayout, amount: t.amount, min: t.minPayout, done: t.payoutRequested }}
        />
      </div>
      <h2 className="mt-10 text-xl font-bold">{t.payoutHistory}</h2>
      <div className="mt-4">
        {payouts.length === 0 ? (
          <Empty text={t.empty} />
        ) : (
          <Table head={[t.date, t.amount, t.payoutMethod, t.status, t.reference]}>
            {payouts.map((p) => (
              <tr key={p.id}>
                <td className="px-4 py-3">{formatDate(p.requestedAt, locale)}</td>
                <td className="px-4 py-3 font-semibold">{formatEgp(p.amountPiasters, locale)}</td>
                <td className="px-4 py-3">{enumLabel(t, "pm", p.method)}</td>
                <td className="px-4 py-3"><StatusBadge t={t} status={p.status} /></td>
                <td className="px-4 py-3" dir="ltr">{p.reference ?? "—"}</td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    </Shell>
  );
}
