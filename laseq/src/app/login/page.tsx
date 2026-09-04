import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { getI18n } from "@/lib/locale";
import { getCurrentUser, homeFor } from "@/lib/auth";
import { PhoneForm } from "./PhoneForm";

export default async function LoginPage() {
  const user = await getCurrentUser();
  if (user) redirect(homeFor(user.role));
  const { t } = await getI18n();
  return (
    <Shell current="/login">
      <div className="mx-auto max-w-md">
        <div className="card">
          <h1 className="text-2xl font-extrabold text-nile-800">{t.loginTitle}</h1>
          <p className="mt-1 text-ink/70">{t.loginSubtitle}</p>
          <PhoneForm labels={{ phone: t.phone, placeholder: t.phonePlaceholder, send: t.sendCode, invalid: t.invalidPhone, rate: t.rateLimited }} />
        </div>
      </div>
    </Shell>
  );
}
