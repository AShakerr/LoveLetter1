"use client";

import { useActionState } from "react";
import { createAdvertiserAction, type FormState } from "@/app/actions/onboarding";
import type { Dict } from "@/lib/i18n";
import { Field } from "@/components/ui";

export function AdvertiserForm({ t, defaultName }: { t: Dict; defaultName: string }) {
  const [state, action, pending] = useActionState<FormState, FormData>(createAdvertiserAction, undefined);
  return (
    <form action={action} className="mt-6 space-y-6">
      <div className="card grid gap-4 md:grid-cols-2">
        <Field label={t.contactName}>
          <input name="name" required defaultValue={defaultName} className="input" />
        </Field>
        <Field label={t.companyName}>
          <input name="companyName" required className="input" />
        </Field>
        <Field label={t.industry}>
          <input name="industry" className="input" />
        </Field>
        <Field label={t.website}>
          <input name="website" className="input" dir="ltr" placeholder="https://" />
        </Field>
        <Field label={t.taxId}>
          <input name="taxId" className="input" dir="ltr" />
        </Field>
      </div>
      {state?.error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700" dir="ltr">{state.error}</p>}
      <button className="btn-primary w-full md:w-auto" disabled={pending}>
        {t.submit}
      </button>
    </form>
  );
}
