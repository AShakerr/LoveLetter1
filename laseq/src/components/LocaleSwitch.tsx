import { setLocaleAction } from "@/app/actions/auth";
import type { Locale } from "@/lib/i18n";
import { getDict } from "@/lib/i18n";

export function LocaleSwitch({ locale, next }: { locale: Locale; next: string }) {
  const t = getDict(locale);
  return (
    <form action={setLocaleAction}>
      <input type="hidden" name="locale" value={locale === "ar" ? "en" : "ar"} />
      <input type="hidden" name="next" value={next} />
      <button type="submit" className="btn-ghost px-3 py-1.5 text-sm" aria-label={t.switchLocale}>
        {t.switchLocale}
      </button>
    </form>
  );
}
