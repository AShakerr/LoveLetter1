"use client";

import { useActionState } from "react";
import { verifyOtpAction, type AuthState } from "@/app/actions/auth";

export function CodeForm({ phone, labels }: { phone: string; labels: { code: string; verify: string; invalid: string } }) {
  const [state, action, pending] = useActionState<AuthState, FormData>(verifyOtpAction, undefined);
  return (
    <form action={action} className="mt-6 space-y-4">
      <input type="hidden" name="phone" value={phone} />
      <label className="block">
        <span className="label">{labels.code}</span>
        <input
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          required
          dir="ltr"
          className="input text-center text-2xl tracking-[0.5em]"
          autoFocus
        />
      </label>
      {state?.error === "INVALID_CODE" && <p className="text-sm text-red-700">{labels.invalid}</p>}
      <button className="btn-primary w-full" disabled={pending}>
        {labels.verify}
      </button>
    </form>
  );
}
