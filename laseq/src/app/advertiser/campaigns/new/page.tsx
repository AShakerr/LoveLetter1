import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireRole } from "@/lib/auth";
import { CITIES } from "@/lib/cities";
import { PageTitle } from "@/components/ui";
import { CampaignForm } from "./CampaignForm";
import { advertiserNav } from "../../_nav";

export default async function NewCampaign() {
  await requireRole("ADVERTISER");
  const { locale, t } = await getI18n();
  return (
    <Shell current="/advertiser/campaigns/new" nav={advertiserNav(t)}>
      <PageTitle title={t.newCampaign} />
      <CampaignForm t={t} locale={locale} cities={CITIES.map((c) => ({ slug: c.slug, name: locale === "ar" ? c.nameAr : c.nameEn }))} />
    </Shell>
  );
}
