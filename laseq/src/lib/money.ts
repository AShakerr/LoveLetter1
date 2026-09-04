/** All money is stored as integer piasters. 100 piasters = 1 EGP. */

export function egpToPiasters(egp: number): number {
  return Math.round(egp * 100);
}

export function piastersToEgp(piasters: number): number {
  return piasters / 100;
}

export function formatEgp(piasters: number, locale: "ar" | "en" = "en"): string {
  const egp = piastersToEgp(piasters);
  const formatted = new Intl.NumberFormat(locale === "ar" ? "ar-EG" : "en-EG", {
    maximumFractionDigits: egp % 1 === 0 ? 0 : 2,
  }).format(egp);
  return locale === "ar" ? `${formatted} ج.م` : `EGP ${formatted}`;
}

export function formatNumber(n: number, locale: "ar" | "en" = "en"): string {
  return new Intl.NumberFormat(locale === "ar" ? "ar-EG" : "en-EG", {
    maximumFractionDigits: 0,
  }).format(n);
}
