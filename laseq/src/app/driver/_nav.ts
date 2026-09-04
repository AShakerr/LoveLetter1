import type { Dict } from "@/lib/i18n";

export function driverNav(t: Dict) {
  return [
    { href: "/driver", label: t.driverHome },
    { href: "/driver/campaigns", label: t.browseCampaigns },
    { href: "/driver/applications", label: t.myApplications },
    { href: "/driver/trips", label: t.trips },
    { href: "/driver/earnings", label: t.earnings },
  ];
}
