"use client";

import { useActionState } from "react";
import { requestOtpAction, type AuthState } from "@/app/actions/auth";

export function PhoneForm({ labels }: { labels: { phone: string; placeholder: string; send: string; invalid: string; rate: string } }) {
  const [state, action, pending] = useActionState<AuthState, FormData>(requestOtpAction, undefined);
  return (
    <form action={action} className="mt-6 space-y-4">
      <label className="block">
        <span className="label">{labels.phone}</span>
        <input name="phone" type="tel" inputMode="tel" dir="ltr" autoComplete="tel" required className="input" placeholder={labels.placeholder} />
      </label>
      {state?.error === "INVALID_PHONE" && <p className="text-sm text-red-700">{labels.invalid}</p>}
      {state?.error === "RATE_LIMITED" && <p className="text-sm text-red-700">{labels.rate}</p>}
      <button className="btn-primary w-full" disabled={pending}>
        {labels.send}
      </button>
    </form>
  );
}
