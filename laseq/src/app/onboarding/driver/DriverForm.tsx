"use client";

import { useActionState } from "react";
import { createDriverProfileAction, type FormState } from "@/app/actions/onboarding";
import type { Dict } from "@/lib/i18n";
import { Field } from "@/components/ui";

const BODY_TYPES = ["SEDAN", "HATCHBACK", "SUV", "MICROBUS", "PICKUP"] as const;
const RIDE = ["NONE", "UBER", "CAREEM", "INDRIVE", "DIDI"] as const;
const PAYOUT = ["VODAFONE_CASH", "INSTAPAY", "BANK_TRANSFER"] as const;

export function DriverForm({ t, cities, defaultName }: { t: Dict; cities: { slug: string; name: string }[]; defaultName: string }) {
  const [state, action, pending] = useActionState<FormState, FormData>(createDriverProfileAction, undefined);
  return (
    <form action={action} className="mt-6 space-y-6">
      <div className="card grid gap-4 md:grid-cols-2">
        <Field label={t.fullName}>
          <input name="name" required defaultValue={defaultName} className="input" />
        </Field>
        <Field label={t.city}>
          <select name="city" required className="input" defaultValue="cairo">
            {cities.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t.rideHailing}>
          <select name="rideHailing" className="input" defaultValue="NONE">
            {RIDE.map((r) => (
              <option key={r} value={r}>
                {t[`rh_${r}`]}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t.avgWeeklyKm}>
          <input name="avgWeeklyKm" type="number" min={0} max={5000} defaultValue={300} className="input" dir="ltr" />
        </Field>
        <Field label={t.payoutMethod}>
          <select name="payoutMethod" className="input" defaultValue="VODAFONE_CASH">
            {PAYOUT.map((p) => (
              <option key={p} value={p}>
                {t[`pm_${p}`]}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t.payoutAccount}>
          <input name="payoutAccount" required className="input" dir="ltr" />
        </Field>
        <Field label={t.licenseNumber}>
          <input name="licenseNumber" className="input" dir="ltr" />
        </Field>
        <Field label={t.nationalId}>
          <input name="nationalId" className="input" dir="ltr" inputMode="numeric" maxLength={14} />
        </Field>
      </div>

      <h2 className="text-xl font-bold">{t.vehicleSection}</h2>
      <div className="card grid gap-4 md:grid-cols-2">
        <Field label={t.make}>
          <input name="make" required className="input" placeholder="Hyundai" />
        </Field>
        <Field label={t.model}>
          <input name="model" required className="input" placeholder="Elantra" />
        </Field>
        <Field label={t.year}>
          <input name="year" type="number" min={1990} max={new Date().getFullYear() + 1} defaultValue={2019} required className="input" dir="ltr" />
        </Field>
        <Field label={t.color}>
          <input name="color" required className="input" />
        </Field>
        <Field label={t.plate}>
          <input name="plate" required className="input" placeholder="أ ب ج ١٢٣٤" />
        </Field>
        <Field label={t.bodyType}>
          <select name="bodyType" className="input" defaultValue="SEDAN">
            {BODY_TYPES.map((b) => (
              <option key={b} value={b}>
                {t[`bt_${b}`]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {state?.error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700" dir="ltr">{state.error}</p>}
      <button className="btn-primary w-full md:w-auto" disabled={pending}>
        {t.submit}
      </button>
    </form>
  );
}
