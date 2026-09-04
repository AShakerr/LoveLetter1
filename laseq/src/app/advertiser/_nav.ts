import type { Dict } from "@/lib/i18n";

export function advertiserNav(t: Dict) {
  return [
    { href: "/advertiser", label: t.advertiserHome },
    { href: "/advertiser/campaigns/new", label: t.newCampaign },
  ];
}
