import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { submitInstallPhotoAction } from "@/app/actions/driver";
import { PageTitle, StatusBadge, Empty, formatDate } from "@/components/ui";
import { driverNav } from "../_nav";

export default async function Applications() {
  const user = await requireRole("DRIVER");
  const { locale, t } = await getI18n();
  const apps = await db.application.findMany({
    where: { driverId: user.driver!.id },
    include: { campaign: { include: { advertiser: true } }, vehicle: true },
    orderBy: { appliedAt: "desc" },
  });
  return (
    <Shell current="/driver/applications" nav={driverNav(t)}>
      <PageTitle title={t.myApplications} />
      {apps.length === 0 && <Empty text={t.empty} />}
      <div className="grid gap-4">
        {apps.map((a) => (
          <div key={a.id} className="card">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <Link href={`/driver/campaigns/${a.campaignId}`} className="text-lg font-bold hover:underline">
                  {a.campaign.name}
                </Link>
                <div className="text-sm text-ink/60">
                  {a.campaign.advertiser.companyName} · {a.vehicle.make} {a.vehicle.model} · {formatDate(a.appliedAt, locale)}
                </div>
              </div>
              <StatusBadge t={t} status={a.status} />
            </div>
            {a.status === "ACCEPTED" && (
              <form action={submitInstallPhotoAction} className="mt-4 rounded-xl bg-sand-100 p-4">
                <input type="hidden" name="applicationId" value={a.id} />
                <p className="text-sm">{t.accepted}</p>
                <p className="mt-1 text-xs text-ink/60">{t.installPhotoHelp}</p>
                {a.note && <p className="mt-1 text-xs text-red-700">{a.note}</p>}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <input type="file" name="photo" accept="image/jpeg,image/png" required className="text-sm" />
                  <button className="btn-primary text-sm">{t.uploadInstallPhoto}</button>
                </div>
              </form>
            )}
            {a.status === "INSTALL_SUBMITTED" && (
              <div className="mt-4 flex items-center gap-4">
                {a.installPhotoUrl && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={a.installPhotoUrl} alt="" className="h-20 w-28 rounded-lg object-cover" />
                )}
                <p className="text-sm text-ink/70">{t.installSubmitted}</p>
              </div>
            )}
            {a.status === "ACTIVE" && (
              <div className="mt-4">
                <Link href={`/driver/trips/new?application=${a.id}`} className="btn-accent text-sm">
                  {t.startTrip}
                </Link>
              </div>
            )}
          </div>
        ))}
      </div>
    </Shell>
  );
}
