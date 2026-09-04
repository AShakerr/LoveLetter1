import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { verifyInstallAction } from "@/app/actions/admin";
import { PageTitle, Empty } from "@/components/ui";
import { adminNav } from "../_nav";

export default async function AdminInstalls() {
  await requireRole("ADMIN");
  const { t } = await getI18n();
  const rows = await db.application.findMany({
    where: { status: "INSTALL_SUBMITTED" },
    include: { campaign: true, driver: { include: { user: true } }, vehicle: true },
    orderBy: { appliedAt: "asc" },
  });
  return (
    <Shell current="/admin/installs" nav={adminNav(t)}>
      <PageTitle title={t.adminInstalls} />
      {rows.length === 0 ? (
        <Empty text={t.empty} />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {rows.map((a) => (
            <div key={a.id} className="card">
              {a.installPhotoUrl && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={a.installPhotoUrl} alt="" className="mb-3 max-h-72 w-full rounded-xl object-cover" />
              )}
              <div className="font-bold">{a.campaign.name}</div>
              <div className="text-sm text-ink/70">
                {a.driver.user.name} · {a.vehicle.make} {a.vehicle.model} {a.vehicle.color} · <span dir="ltr">{a.vehicle.plate}</span>
              </div>
              <form action={verifyInstallAction} className="mt-4 flex items-center gap-2">
                <input type="hidden" name="id" value={a.id} />
                <input name="note" placeholder={t.note} className="input flex-1 py-1 text-xs" />
                <button name="decision" value="approve" className="btn-primary px-3 py-1 text-xs">{t.verifyInstall}</button>
                <button name="decision" value="reject" className="btn-danger px-3 py-1 text-xs">{t.reject}</button>
              </form>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
