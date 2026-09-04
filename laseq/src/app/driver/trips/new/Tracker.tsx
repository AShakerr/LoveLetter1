"use client";

import { useEffect, useRef, useState } from "react";
import { haversineKm } from "@/lib/geo";
import { formatEgp } from "@/lib/money";

type Labels = Record<
  "choose" | "start" | "end" | "tracking" | "distance" | "pings" | "denied" | "summary" | "km" | "impressions" | "earnings" | "flagged",
  string
>;

interface Summary {
  status: string;
  distanceKm: number;
  estImpressions: number;
  earningsPiasters: number;
  flagReasons: string[];
}

const FLUSH_EVERY_MS = 10_000;

export function Tracker({
  locale,
  applications,
  initialApplicationId,
  openTrip,
  labels,
}: {
  locale: "ar" | "en";
  applications: { id: string; name: string }[];
  initialApplicationId: string;
  openTrip: { id: string; startedAt: string } | null;
  labels: Labels;
}) {
  const [applicationId, setApplicationId] = useState(initialApplicationId);
  const [tripId, setTripId] = useState<string | null>(openTrip?.id ?? null);
  const [distance, setDistance] = useState(0);
  const [sent, setSent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [busy, setBusy] = useState(false);

  const buffer = useRef<{ lat: number; lng: number; speedKmh: number | null; recordedAt: number }[]>([]);
  const last = useRef<{ lat: number; lng: number } | null>(null);
  const watchId = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function flush(id: string) {
    if (buffer.current.length === 0) return;
    const pings = buffer.current.splice(0, buffer.current.length);
    const res = await fetch(`/api/trips/${id}/pings`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pings }),
    });
    if (res.ok) setSent((n) => n + pings.length);
  }

  function stopWatching() {
    if (watchId.current !== null) navigator.geolocation.clearWatch(watchId.current);
    if (timer.current) clearInterval(timer.current);
    watchId.current = null;
    timer.current = null;
  }

  function startWatching(id: string) {
    if (!("geolocation" in navigator)) {
      setError(labels.denied);
      return;
    }
    watchId.current = navigator.geolocation.watchPosition(
      (pos) => {
        const p = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        if (last.current) setDistance((d) => d + haversineKm(last.current!, p));
        last.current = p;
        buffer.current.push({
          ...p,
          speedKmh: pos.coords.speed != null ? pos.coords.speed * 3.6 : null,
          recordedAt: pos.timestamp,
        });
      },
      () => setError(labels.denied),
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 },
    );
    timer.current = setInterval(() => void flush(id), FLUSH_EVERY_MS);
  }

  // Resume an in-progress trip on mount: subscribing to the geolocation watch is
  // exactly the "external system" case effects exist for.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (openTrip) startWatching(openTrip.id);
    return stopWatching;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function start() {
    setBusy(true);
    setError(null);
    setSummary(null);
    const res = await fetch("/api/trips", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ applicationId }),
    });
    setBusy(false);
    if (!res.ok) {
      setError(`Could not start trip (${res.status})`);
      return;
    }
    const data = (await res.json()) as { tripId: string };
    setTripId(data.tripId);
    setDistance(0);
    setSent(0);
    last.current = null;
    startWatching(data.tripId);
  }

  async function end() {
    if (!tripId) return;
    setBusy(true);
    stopWatching();
    await flush(tripId);
    const res = await fetch(`/api/trips/${tripId}/end`, { method: "POST" });
    setBusy(false);
    if (res.ok) setSummary((await res.json()) as Summary);
    setTripId(null);
  }

  return (
    <div className="mx-auto max-w-md space-y-4">
      {!tripId && (
        <label className="block">
          <span className="label">{labels.choose}</span>
          <select className="input" value={applicationId} onChange={(e) => setApplicationId(e.target.value)}>
            {applications.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
      )}

      {tripId ? (
        <div className="card text-center">
          <div className="animate-pulse text-sm font-semibold text-emerald-700">● {labels.tracking}</div>
          <div className="mt-4 text-5xl font-extrabold text-nile-800">{distance.toFixed(2)}</div>
          <div className="text-ink/60">{labels.distance} ({labels.km})</div>
          <div className="mt-2 text-xs text-ink/50">
            {labels.pings}: {sent}
          </div>
          <button onClick={end} disabled={busy} className="btn-danger mt-6 w-full">
            {labels.end}
          </button>
        </div>
      ) : (
        <button onClick={start} disabled={busy} className="btn-accent w-full text-lg">
          {labels.start}
        </button>
      )}

      {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {summary && (
        <div className="card">
          <h2 className="font-bold">{labels.summary}</h2>
          <dl className="mt-3 grid grid-cols-3 gap-3 text-center">
            <div>
              <dt className="text-xs text-ink/60">{labels.km}</dt>
              <dd className="text-xl font-extrabold">{summary.distanceKm.toFixed(1)}</dd>
            </div>
            <div>
              <dt className="text-xs text-ink/60">{labels.impressions}</dt>
              <dd className="text-xl font-extrabold">{summary.estImpressions.toLocaleString(locale === "ar" ? "ar-EG" : "en-EG")}</dd>
            </div>
            <div>
              <dt className="text-xs text-ink/60">{labels.earnings}</dt>
              <dd className="text-xl font-extrabold text-nile-800">{formatEgp(summary.earningsPiasters, locale)}</dd>
            </div>
          </dl>
          {summary.status === "FLAGGED" && (
            <p className="mt-3 rounded-lg bg-red-50 p-2 text-sm text-red-700">
              {labels.flagged}: <span dir="ltr">{summary.flagReasons.join(", ")}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
