/**
 * Egyptian cities Laseq launches in.
 *
 * `impressionsPerKm` is a heuristic for how many people see a car ad per km
 * driven in that city. It is derived from traffic density and average speed:
 * Greater Cairo traffic averages ~20 km/h with extremely dense mixed traffic and
 * sidewalks, so a moving car is seen by far more people per km than in Hurghada.
 * These numbers are the pricing baseline and should be tuned against real
 * campaign data (brand lift surveys, QR scans) once live.
 *
 * `bounds` is a coarse bounding box used for geofence sanity checks on trips.
 */
export type CitySlug =
  | "cairo"
  | "giza"
  | "alexandria"
  | "mansoura"
  | "tanta"
  | "port-said"
  | "suez"
  | "ismailia"
  | "hurghada"
  | "luxor"
  | "aswan";

export interface City {
  slug: CitySlug;
  nameEn: string;
  nameAr: string;
  impressionsPerKm: number;
  bounds: { minLat: number; maxLat: number; minLng: number; maxLng: number };
}

export const CITIES: City[] = [
  {
    slug: "cairo",
    nameEn: "Cairo",
    nameAr: "القاهرة",
    impressionsPerKm: 950,
    bounds: { minLat: 29.75, maxLat: 30.25, minLng: 31.15, maxLng: 31.75 },
  },
  {
    slug: "giza",
    nameEn: "Giza & 6th of October",
    nameAr: "الجيزة و٦ أكتوبر",
    impressionsPerKm: 850,
    bounds: { minLat: 29.75, maxLat: 30.2, minLng: 30.8, maxLng: 31.3 },
  },
  {
    slug: "alexandria",
    nameEn: "Alexandria",
    nameAr: "الإسكندرية",
    impressionsPerKm: 750,
    bounds: { minLat: 31.05, maxLat: 31.4, minLng: 29.7, maxLng: 30.15 },
  },
  {
    slug: "mansoura",
    nameEn: "Mansoura",
    nameAr: "المنصورة",
    impressionsPerKm: 500,
    bounds: { minLat: 30.98, maxLat: 31.1, minLng: 31.3, maxLng: 31.45 },
  },
  {
    slug: "tanta",
    nameEn: "Tanta",
    nameAr: "طنطا",
    impressionsPerKm: 480,
    bounds: { minLat: 30.74, maxLat: 30.84, minLng: 30.95, maxLng: 31.05 },
  },
  {
    slug: "port-said",
    nameEn: "Port Said",
    nameAr: "بورسعيد",
    impressionsPerKm: 420,
    bounds: { minLat: 31.2, maxLat: 31.32, minLng: 32.25, maxLng: 32.35 },
  },
  {
    slug: "suez",
    nameEn: "Suez",
    nameAr: "السويس",
    impressionsPerKm: 380,
    bounds: { minLat: 29.9, maxLat: 30.05, minLng: 32.45, maxLng: 32.6 },
  },
  {
    slug: "ismailia",
    nameEn: "Ismailia",
    nameAr: "الإسماعيلية",
    impressionsPerKm: 380,
    bounds: { minLat: 30.55, maxLat: 30.65, minLng: 32.2, maxLng: 32.35 },
  },
  {
    slug: "hurghada",
    nameEn: "Hurghada",
    nameAr: "الغردقة",
    impressionsPerKm: 350,
    bounds: { minLat: 27.1, maxLat: 27.35, minLng: 33.7, maxLng: 33.9 },
  },
  {
    slug: "luxor",
    nameEn: "Luxor",
    nameAr: "الأقصر",
    impressionsPerKm: 330,
    bounds: { minLat: 25.65, maxLat: 25.75, minLng: 32.6, maxLng: 32.7 },
  },
  {
    slug: "aswan",
    nameEn: "Aswan",
    nameAr: "أسوان",
    impressionsPerKm: 300,
    bounds: { minLat: 24.05, maxLat: 24.15, minLng: 32.85, maxLng: 32.95 },
  },
];

export const CITY_BY_SLUG: Record<string, City> = Object.fromEntries(
  CITIES.map((c) => [c.slug, c]),
);

export function cityName(slug: string, locale: "ar" | "en"): string {
  const c = CITY_BY_SLUG[slug];
  if (!c) return slug;
  return locale === "ar" ? c.nameAr : c.nameEn;
}

export function parseCities(json: string): CitySlug[] {
  try {
    const arr = JSON.parse(json);
    return Array.isArray(arr) ? arr.filter((s) => s in CITY_BY_SLUG) : [];
  } catch {
    return [];
  }
}

export function isInsideCity(lat: number, lng: number, slug: string): boolean {
  const c = CITY_BY_SLUG[slug];
  if (!c) return false;
  const b = c.bounds;
  return lat >= b.minLat && lat <= b.maxLat && lng >= b.minLng && lng <= b.maxLng;
}

export function isInsideAnyCity(lat: number, lng: number, slugs: string[]): boolean {
  return slugs.some((s) => isInsideCity(lat, lng, s));
}
