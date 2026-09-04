import Link from "next/link";
import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { normalizeEgyptPhone, displayPhone } from "@/lib/phone";
import { isDevOtp } from "@/lib/otp";
import { CodeForm } from "./CodeForm";

export default async function VerifyPage({ searchParams }: { searchParams: Promise<{ phone?: string }> }) {
  const { phone: raw } = await searchParams;
  const phone = normalizeEgyptPhone(raw ?? "");
  if (!phone) redirect("/login");
  const { t } = await getI18n();
  return (
    <Shell current="/login">
      <div className="mx-auto max-w-md">
        <div className="card">
          <h1 className="text-2xl font-extrabold text-nile-800">{t.codeTitle}</h1>
          <p className="mt-1 text-ink/70">
            {t.codeSentTo} <span dir="ltr" className="font-semibold">{displayPhone(phone)}</span>
          </p>
          {isDevOtp() && <p className="mt-2 rounded-lg bg-amber-100 p-2 text-xs text-amber-900">{t.devCodeHint}</p>}
          <CodeForm phone={phone} labels={{ code: t.codeLabel, verify: t.verify, invalid: t.invalidCode }} />
          <Link href="/login" className="mt-4 inline-block text-sm text-nile-700 underline">
            {t.changeNumber}
          </Link>
        </div>
      </div>
    </Shell>
  );
}
