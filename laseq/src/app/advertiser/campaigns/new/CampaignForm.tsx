"use client";

import { useActionState, useState } from "react";
import { createCampaignAction } from "@/app/actions/advertiser";
import type { FormState } from "@/app/actions/onboarding";
import type { Dict } from "@/lib/i18n";
import { Field } from "@/components/ui";
import { estimateMonthlyCampaignImpressions, cpmPiasters } from "@/lib/impressions";
import { advertiserCostPerKmPiasters } from "@/lib/earnings";
import { egpToPiasters, formatEgp, formatNumber } from "@/lib/money";

const PLACEMENTS = ["REAR_WINDOW", "SIDE_DOORS", "FULL_WRAP"] as const;
const BODY_TYPES = ["SEDAN", "HATCHBACK", "SUV", "MICROBUS", "PICKUP"] as const;
const ASSUMED_WEEKLY_KM = 350;

function isoDate(d: Date) {
  return d.toISOString().slice(0, 10);
}

export function CampaignForm({ t, locale, cities }: { t: Dict; locale: "ar" | "en"; cities: { slug: string; name: string }[] }) {
  const [state, action, pending] = useActionState<FormState, FormData>(createCampaignAction, undefined);
  const [placement, setPlacement] = useState<(typeof PLACEMENTS)[number]>("REAR_WINDOW");
  const [selectedCities, setSelectedCities] = useState<string[]>(["cairo", "giza"]);
  const [slots, setSlots] = useState(20);
  const [rateEgp, setRateEgp] = useState(1.5);
  const [baseEgp, setBaseEgp] = useState(500);
  const [capKm, setCapKm] = useState(1500);

  const monthlyKm = Math.min(ASSUMED_WEEKLY_KM * 4.3, capKm);
  const impressions = estimateMonthlyCampaignImpressions({
    driverSlots: slots,
    avgWeeklyKm: ASSUMED_WEEKLY_KM,
    monthlyCapKm: capKm,
    citySlugs: selectedCities,
    placement,
  });
  const costPerKm = advertiserCostPerKmPiasters(egpToPiasters(rateEgp));
  const monthlyCost = Math.round(slots * (monthlyKm * costPerKm + egpToPiasters(baseEgp) / 0.7));
  const cpm = cpmPiasters(monthlyCost, impressions);

  const today = new Date();
  const inMonth = new Date(today.getTime() + 30 * 86400000);
  const inFourMonths = new Date(today.getTime() + 120 * 86400000);

  return (
    <form action={action} className="grid gap-6 md:grid-cols-3">
      <div className="space-y-6 md:col-span-2">
        <div className="card grid gap-4">
          <Field label={t.campaignName}>
            <input name="name" required className="input" />
          </Field>
          <Field label={t.description}>
            <textarea name="description" required rows={4} className="input" />
          </Field>
          <Field label={t.creativeUrl}>
            <input name="creativeUrl" className="input" dir="ltr" placeholder="https://…/sticker.png" />
          </Field>
          <Field label={t.placement}>
            <select name="placement" className="input" value={placement} onChange={(e) => setPlacement(e.target.value as typeof placement)}>
              {PLACEMENTS.map((p) => (
                <option key={p} value={p}>
                  {t[`pl_${p}`]}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div className="card">
          <div className="label">{t.cities}</div>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
            {cities.map((c) => (
              <label key={c.slug} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  name="cities"
                  value={c.slug}
                  checked={selectedCities.includes(c.slug)}
                  onChange={(e) =>
                    setSelectedCities((prev) => (e.target.checked ? [...prev, c.slug] : prev.filter((s) => s !== c.slug)))
                  }
                />
                {c.name}
              </label>
            ))}
          </div>
        </div>

        <div className="card grid gap-4 md:grid-cols-2">
          <Field label={t.driverSlots}>
            <input name="driverSlots" type="number" min={1} max={5000} value={slots} onChange={(e) => setSlots(Number(e.target.value) || 0)} className="input" dir="ltr" />
          </Field>
          <Field label={`${t.ratePerKm} (EGP)`}>
            <input name="ratePerKmEgp" type="number" min={0.25} max={50} step={0.05} value={rateEgp} onChange={(e) => setRateEgp(Number(e.target.value) || 0)} className="input" dir="ltr" />
          </Field>
          <Field label={`${t.monthlyBase} (EGP)`}>
            <input name="monthlyBaseEgp" type="number" min={0} max={20000} step={50} value={baseEgp} onChange={(e) => setBaseEgp(Number(e.target.value) || 0)} className="input" dir="ltr" />
          </Field>
          <Field label={t.monthlyCap}>
            <input name="monthlyCapKm" type="number" min={100} max={10000} step={50} value={capKm} onChange={(e) => setCapKm(Number(e.target.value) || 0)} className="input" dir="ltr" />
          </Field>
          <Field label={`${t.budget} (EGP)`}>
            <input name="budgetEgp" type="number" min={1000} step={500} defaultValue={Math.max(1000, Math.round((monthlyCost / 100) * 3))} className="input" dir="ltr" />
          </Field>
          <Field label={t.minCarYear}>
            <input name="minCarYear" type="number" min={1990} max={today.getFullYear() + 1} defaultValue={2012} className="input" dir="ltr" />
          </Field>
          <Field label={t.startDate}>
            <input name="startDate" type="date" defaultValue={isoDate(inMonth)} required className="input" dir="ltr" />
          </Field>
          <Field label={t.endDate}>
            <input name="endDate" type="date" defaultValue={isoDate(inFourMonths)} required className="input" dir="ltr" />
          </Field>
          <div className="md:col-span-2">
            <div className="label">{t.allowedBodyTypes}</div>
            <div className="flex flex-wrap gap-3">
              {BODY_TYPES.map((b) => (
                <label key={b} className="flex items-center gap-2 text-sm">
                  <input type="checkbox" name="allowedBodyTypes" value={b} />
                  {t[`bt_${b}`]}
                </label>
              ))}
            </div>
          </div>
        </div>

        {state?.error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700" dir="ltr">{state.error}</p>}
        <div className="flex flex-wrap gap-3">
          <button name="intent" value="submit" className="btn-accent" disabled={pending}>
            {t.submitForReview}
          </button>
          <button name="intent" value="draft" className="btn-ghost" disabled={pending}>
            {t.saveDraft}
          </button>
        </div>
      </div>

      <aside className="card h-fit md:sticky md:top-24">
        <h2 className="font-bold">{t.estimateTitle}</h2>
        <dl className="mt-4 space-y-4">
          <div>
            <dt className="text-sm text-ink/60">{t.estMonthlyImpressions}</dt>
            <dd className="text-2xl font-extrabold text-nile-800">{formatNumber(impressions, locale)}</dd>
          </div>
          <div>
            <dt className="text-sm text-ink/60">{t.estMonthlyCost}</dt>
            <dd className="text-2xl font-extrabold text-nile-800">{formatEgp(monthlyCost, locale)}</dd>
          </div>
          <div>
            <dt className="text-sm text-ink/60">{t.costPerKm}</dt>
            <dd className="font-semibold">{formatEgp(costPerKm, locale)}</dd>
          </div>
          <div>
            <dt className="text-sm text-ink/60">{t.estCpm}</dt>
            <dd className="font-semibold">{formatEgp(cpm, locale)}</dd>
          </div>
        </dl>
      </aside>
    </form>
  );
}
