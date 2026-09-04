import Link from "next/link";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { CITIES } from "@/lib/cities";

export default async function Landing() {
  const { locale, t } = await getI18n();
  const nav = [
    { href: "#drivers", label: t.navDrivers },
    { href: "#brands", label: t.navBrands },
    { href: "#why", label: t.navHow },
  ];
  return (
    <Shell current="/" nav={nav}>
      <section className="grid items-center gap-10 py-8 md:grid-cols-2 md:py-16">
        <div>
          <div className="mb-3 inline-block rounded-full bg-amber-brand/20 px-3 py-1 text-sm font-semibold text-nile-800">
            {t.heroKicker}
          </div>
          <h1 className="text-4xl font-extrabold leading-tight text-nile-800 md:text-5xl">{t.heroTitle}</h1>
          <p className="mt-4 text-lg text-ink/70">{t.heroSubtitle}</p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/login?as=driver" className="btn-accent text-base">
              {t.heroCtaDriver}
            </Link>
            <Link href="/login?as=brand" className="btn-primary text-base">
              {t.heroCtaBrand}
            </Link>
          </div>
        </div>
        <CarIllustration />
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Stat label={t.statDriversEarn} value={t.statDriversEarnValue} />
        <Stat label={t.statCpm} value={t.statCpmValue} />
        <Stat label={t.statCities} value={t.statCitiesValue} />
      </section>

      <section id="drivers" className="py-16">
        <h2 className="text-3xl font-extrabold text-nile-800">{t.driversTitle}</h2>
        <div className="mt-8 grid gap-5 md:grid-cols-3">
          <Step n="1" title={t.driversStep1Title} body={t.driversStep1Body} />
          <Step n="2" title={t.driversStep2Title} body={t.driversStep2Body} />
          <Step n="3" title={t.driversStep3Title} body={t.driversStep3Body} />
        </div>
        <Link href="/login?as=driver" className="btn-accent mt-8">
          {t.heroCtaDriver}
        </Link>
      </section>

      <section id="brands" className="rounded-3xl bg-nile-800 px-6 py-14 text-white md:px-12">
        <h2 className="text-3xl font-extrabold">{t.brandsTitle}</h2>
        <div className="mt-8 grid gap-5 md:grid-cols-3">
          <Step n="1" title={t.brandsStep1Title} body={t.brandsStep1Body} dark />
          <Step n="2" title={t.brandsStep2Title} body={t.brandsStep2Body} dark />
          <Step n="3" title={t.brandsStep3Title} body={t.brandsStep3Body} dark />
        </div>
        <Link href="/login?as=brand" className="btn-accent mt-8">
          {t.heroCtaBrand}
        </Link>
      </section>

      <section id="why" className="py-16">
        <h2 className="text-3xl font-extrabold text-nile-800">{t.whyTitle}</h2>
        <ul className="mt-6 grid gap-4 md:grid-cols-3">
          {[t.why1, t.why2, t.why3].map((w, i) => (
            <li key={i} className="card text-ink/80">
              {w}
            </li>
          ))}
        </ul>
        <div className="mt-8 flex flex-wrap gap-2">
          {CITIES.map((c) => (
            <span key={c.slug} className="badge bg-sand-100 text-nile-800">
              {locale === "ar" ? c.nameAr : c.nameEn}
            </span>
          ))}
        </div>
      </section>

      <section className="pb-16">
        <h2 className="text-3xl font-extrabold text-nile-800">{t.faqTitle}</h2>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <Faq q={t.faq1q} a={t.faq1a} />
          <Faq q={t.faq2q} a={t.faq2a} />
          <Faq q={t.faq3q} a={t.faq3a} />
        </div>
      </section>
    </Shell>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card">
      <div className="text-sm text-ink/60">{label}</div>
      <div className="mt-1 text-3xl font-extrabold text-nile-800">{value}</div>
    </div>
  );
}

function Step({ n, title, body, dark }: { n: string; title: string; body: string; dark?: boolean }) {
  return (
    <div className={dark ? "rounded-2xl bg-white/10 p-5" : "card"}>
      <div className={`mb-3 inline-flex h-9 w-9 items-center justify-center rounded-full text-lg font-extrabold ${dark ? "bg-amber-brand text-ink" : "bg-nile-700 text-white"}`}>
        {n}
      </div>
      <h3 className="text-lg font-bold">{title}</h3>
      <p className={`mt-1 ${dark ? "text-white/80" : "text-ink/70"}`}>{body}</p>
    </div>
  );
}

function Faq({ q, a }: { q: string; a: string }) {
  return (
    <div className="card">
      <h3 className="font-bold">{q}</h3>
      <p className="mt-2 text-ink/70">{a}</p>
    </div>
  );
}

function CarIllustration() {
  return (
    <div className="relative mx-auto w-full max-w-md">
      <svg viewBox="0 0 400 220" className="w-full" role="img" aria-label="Car with an ad sticker">
        <rect x="0" y="180" width="400" height="6" rx="3" fill="#e9dcc0" />
        <path d="M40 150 L70 100 Q80 85 100 85 L250 85 Q275 85 295 105 L340 140 L360 150 Z" fill="#0f6e8c" />
        <path d="M110 92 L235 92 Q255 92 270 108 L290 130 L100 130 Z" fill="#cfe8f0" />
        <rect x="120" y="135" width="150" height="34" rx="6" fill="#f2a900" />
        <text x="195" y="158" textAnchor="middle" fontSize="18" fontWeight="800" fill="#1b1a17">
          YOUR AD HERE
        </text>
        <circle cx="105" cy="160" r="22" fill="#1b1a17" />
        <circle cx="105" cy="160" r="10" fill="#e9dcc0" />
        <circle cx="300" cy="160" r="22" fill="#1b1a17" />
        <circle cx="300" cy="160" r="10" fill="#e9dcc0" />
      </svg>
    </div>
  );
}
