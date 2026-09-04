import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireUser } from "@/lib/auth";
import { CITIES } from "@/lib/cities";
import { DriverForm } from "./DriverForm";

export default async function DriverOnboarding() {
  const user = await requireUser();
  if (user.driver) redirect("/driver");
  if (user.advertiser) redirect("/advertiser");
  const { locale, t } = await getI18n();
  return (
    <Shell current="/onboarding/driver">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-3xl font-extrabold text-nile-800">{t.driverOnboardingTitle}</h1>
        <DriverForm
          t={t}
          cities={CITIES.map((c) => ({ slug: c.slug, name: locale === "ar" ? c.nameAr : c.nameEn }))}
          defaultName={user.name ?? ""}
        />
      </div>
    </Shell>
  );
}
