import type { Dict } from "@/lib/i18n";
import { enumLabel } from "@/lib/i18n";

export function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card">
      <div className="text-sm text-ink/60">{label}</div>
      <div className="mt-1 text-2xl font-extrabold text-nile-800">{value}</div>
      {hint && <div className="mt-1 text-xs text-ink/50">{hint}</div>}
    </div>
  );
}

const STATUS_COLORS: Record<string, string> = {
  PENDING: "bg-amber-100 text-amber-800",
  PENDING_REVIEW: "bg-amber-100 text-amber-800",
  APPLIED: "bg-amber-100 text-amber-800",
  REQUESTED: "bg-amber-100 text-amber-800",
  INSTALL_SUBMITTED: "bg-blue-100 text-blue-800",
  ACCEPTED: "bg-blue-100 text-blue-800",
  DRAFT: "bg-gray-100 text-gray-700",
  APPROVED: "bg-emerald-100 text-emerald-800",
  ACTIVE: "bg-emerald-100 text-emerald-800",
  PAID: "bg-emerald-100 text-emerald-800",
  COMPLETED: "bg-gray-100 text-gray-700",
  ENDED: "bg-gray-100 text-gray-700",
  PAUSED: "bg-gray-100 text-gray-700",
  REJECTED: "bg-red-100 text-red-800",
  FLAGGED: "bg-red-100 text-red-800",
};

export function StatusBadge({ t, status }: { t: Dict; status: string }) {
  return <span className={`badge ${STATUS_COLORS[status] ?? "bg-gray-100 text-gray-700"}`}>{enumLabel(t, "st", status)}</span>;
}

export function PageTitle({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-extrabold text-nile-800 md:text-3xl">{title}</h1>
        {subtitle && <p className="mt-1 text-ink/70">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="card text-center text-ink/60">{text}</div>;
}

export function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink/50">{hint}</span>}
    </label>
  );
}

export function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead className="bg-sand-100 text-start text-xs uppercase tracking-wide text-ink/60">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-4 py-3 text-start font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-sand-200">{children}</tbody>
      </table>
    </div>
  );
}

export function formatDate(d: Date, locale: "ar" | "en") {
  return new Intl.DateTimeFormat(locale === "ar" ? "ar-EG" : "en-GB", { dateStyle: "medium", timeZone: "Africa/Cairo" }).format(d);
}

export function formatDateTime(d: Date, locale: "ar" | "en") {
  return new Intl.DateTimeFormat(locale === "ar" ? "ar-EG" : "en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Africa/Cairo",
  }).format(d);
}
