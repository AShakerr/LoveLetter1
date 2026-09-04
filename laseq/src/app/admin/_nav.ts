import type { Dict } from "@/lib/i18n";

export function adminNav(t: Dict) {
  return [
    { href: "/admin", label: t.overview },
    { href: "/admin/drivers", label: t.adminDrivers },
    { href: "/admin/advertisers", label: t.adminAdvertisers },
    { href: "/admin/campaigns", label: t.adminCampaigns },
    { href: "/admin/installs", label: t.adminInstalls },
    { href: "/admin/payouts", label: t.adminPayouts },
    { href: "/admin/trips", label: t.adminTrips },
  ];
}
