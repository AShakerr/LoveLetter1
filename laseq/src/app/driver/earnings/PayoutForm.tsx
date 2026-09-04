"use client";

import { useActionState } from "react";
import { requestPayoutAction, type ActionResult } from "@/app/actions/driver";

export function PayoutForm({ balanceEgp, labels }: { balanceEgp: number; labels: { request: string; amount: string; min: string; done: string } }) {
  const [state, action, pending] = useActionState<ActionResult | undefined, FormData>(requestPayoutAction, undefined);
  const canRequest = balanceEgp >= 100;
  return (
    <form action={action} className="card flex flex-wrap items-end gap-3">
      <label className="block flex-1">
        <span className="label">{labels.amount} (EGP)</span>
        <input name="amount" type="number" min={100} max={Math.floor(balanceEgp)} step={1} defaultValue={Math.floor(balanceEgp)} className="input" dir="ltr" disabled={!canRequest} />
      </label>
      <button className="btn-primary" disabled={pending || !canRequest}>
        {labels.request}
      </button>
      <p className="w-full text-xs text-ink/60">{labels.min}</p>
      {state?.ok && <p className="w-full text-sm text-emerald-700">{labels.done}</p>}
      {state && !state.ok && <p className="w-full text-sm text-red-700" dir="ltr">{state.error}</p>}
    </form>
  );
}
