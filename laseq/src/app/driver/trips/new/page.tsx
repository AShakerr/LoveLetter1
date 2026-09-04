import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { db } from "@/lib/db";
import { PageTitle, Empty } from "@/components/ui";
import { Tracker } from "./Tracker";
import { driverNav } from "../../_nav";

export default async function NewTrip({ searchParams }: { searchParams: Promise<{ application?: string }> }) {
  const { application } = await searchParams;
  const user = await requireRole("DRIVER");
  const { locale, t } = await getI18n();
  const apps = await db.application.findMany({
    where: { driverId: user.driver!.id, status: "ACTIVE", campaign: { status: "ACTIVE" } },
    include: { campaign: true },
  });
  const openTrip = await db.trip.findFirst({
    where: { status: "ACTIVE", application: { driverId: user.driver!.id } },
    select: { id: true, applicationId: true, startedAt: true },
  });
  return (
    <Shell current="/driver/trips/new" nav={driverNav(t)}>
      <PageTitle title={t.trackerTitle} subtitle={t.trackerHelp} />
      {apps.length === 0 ? (
        <Empty text={t.noTrips} />
      ) : (
        <Tracker
          locale={locale}
          applications={apps.map((a) => ({ id: a.id, name: a.campaign.name }))}
          initialApplicationId={application ?? openTrip?.applicationId ?? apps[0].id}
          openTrip={openTrip ? { id: openTrip.id, startedAt: openTrip.startedAt.toISOString() } : null}
          labels={{
            choose: t.chooseCampaign,
            start: t.startTrip,
            end: t.endTrip,
            tracking: t.tracking,
            distance: t.distanceSoFar,
            pings: t.pingsSent,
            denied: t.geoDenied,
            summary: t.tripSummary,
            km: t.km,
            impressions: t.impressions,
            earnings: t.earnings,
            flagged: t.flagged,
          }}
        />
      )}
    </Shell>
  );
}
