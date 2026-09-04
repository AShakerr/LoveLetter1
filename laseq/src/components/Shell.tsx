import Link from "next/link";
import { getI18n } from "@/lib/locale";
import { getCurrentUser, homeFor } from "@/lib/auth";
import { logoutAction } from "@/app/actions/auth";
import { LocaleSwitch } from "./LocaleSwitch";

export async function Shell({
  children,
  current,
  nav,
}: {
  children: React.ReactNode;
  current: string;
  nav?: { href: string; label: string }[];
}) {
  const { locale, t } = await getI18n();
  const user = await getCurrentUser();
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-10 border-b border-sand-200 bg-sand-50/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <Link href="/" className="flex items-center gap-2 text-xl font-extrabold text-nile-800">
            <span className="inline-block h-7 w-7 rounded-lg bg-amber-brand" aria-hidden />
            {t.appName}
          </Link>
          <nav className="hidden items-center gap-5 text-sm font-semibold md:flex">
            {(nav ?? []).map((n) => (
              <Link key={n.href} href={n.href} className="hover:text-nile-700">
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            <LocaleSwitch locale={locale} next={current} />
            {user ? (
              <>
                <Link href={homeFor(user.role)} className="btn-ghost px-3 py-1.5 text-sm">
                  {t.navDashboard}
                </Link>
                <form action={logoutAction}>
                  <button className="btn-ghost px-3 py-1.5 text-sm">{t.logout}</button>
                </form>
              </>
            ) : (
              <Link href="/login" className="btn-primary px-3 py-1.5 text-sm">
                {t.login}
              </Link>
            )}
          </div>
        </div>
        {nav && nav.length > 0 && (
          <div className="mx-auto flex max-w-6xl gap-4 overflow-x-auto px-4 pb-2 text-sm font-semibold md:hidden">
            {nav.map((n) => (
              <Link key={n.href} href={n.href} className="whitespace-nowrap hover:text-nile-700">
                {n.label}
              </Link>
            ))}
          </div>
        )}
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">{children}</main>
      <footer className="border-t border-sand-200 py-6 text-center text-sm text-ink/60">{t.footer}</footer>
    </div>
  );
}
