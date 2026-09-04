import Link from "next/link";
import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { requireUser, homeFor } from "@/lib/auth";

export default async function OnboardingPage() {
  const user = await requireUser();
  if (user.role) redirect(homeFor(user.role));
  const { t } = await getI18n();
  return (
    <Shell current="/onboarding">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-center text-3xl font-extrabold text-nile-800">{t.onboardingTitle}</h1>
        <div className="mt-8 grid gap-4 md:grid-cols-2">
          <Link href="/onboarding/driver" className="card block hover:border-nile-600">
            <div className="text-4xl">🚗</div>
            <h2 className="mt-3 text-xl font-bold">{t.roleDriverTitle}</h2>
            <p className="mt-1 text-ink/70">{t.roleDriverBody}</p>
          </Link>
          <Link href="/onboarding/advertiser" className="card block hover:border-nile-600">
            <div className="text-4xl">📣</div>
            <h2 className="mt-3 text-xl font-bold">{t.roleAdvertiserTitle}</h2>
            <p className="mt-1 text-ink/70">{t.roleAdvertiserBody}</p>
          </Link>
        </div>
      </div>
    </Shell>
  );
}
