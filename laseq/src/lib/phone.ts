/**
 * Normalize Egyptian mobile numbers to E.164 (+20XXXXXXXXXX).
 * Accepts: 01012345678, +201012345678, 00201012345678, 201012345678,
 * with spaces/dashes, and Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩).
 */
export function normalizeEgyptPhone(input: string): string | null {
  const arabicDigits = "٠١٢٣٤٥٦٧٨٩";
  let s = "";
  for (const ch of input.trim()) {
    const idx = arabicDigits.indexOf(ch);
    if (idx >= 0) s += String(idx);
    else if (/[0-9+]/.test(ch)) s += ch;
  }
  if (s.startsWith("+")) s = s.slice(1);
  if (s.startsWith("0020")) s = s.slice(4);
  else if (s.startsWith("00")) return null;
  if (s.startsWith("20")) s = s.slice(2);
  if (s.startsWith("0")) s = s.slice(1);
  // Egyptian mobiles: 10 digits starting with 10, 11, 12 or 15
  if (!/^1[0125]\d{8}$/.test(s)) return null;
  return `+20${s}`;
}

export function displayPhone(e164: string): string {
  if (!e164.startsWith("+20")) return e164;
  const local = "0" + e164.slice(3);
  return `${local.slice(0, 4)} ${local.slice(4, 7)} ${local.slice(7)}`;
}
