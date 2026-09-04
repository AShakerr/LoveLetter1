import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireUser } from "@/lib/auth";
import { AdvertiserForm } from "./AdvertiserForm";

export default async function AdvertiserOnboarding() {
  const user = await requireUser();
  if (user.advertiser) redirect("/advertiser");
  if (user.driver) redirect("/driver");
  const { t } = await getI18n();
  return (
    <Shell current="/onboarding/advertiser">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-3xl font-extrabold text-nile-800">{t.advertiserOnboardingTitle}</h1>
        <AdvertiserForm t={t} defaultName={user.name ?? ""} />
      </div>
    </Shell>
  );
}
