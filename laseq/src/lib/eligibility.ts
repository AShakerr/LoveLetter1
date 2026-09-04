import { parseCities } from "./cities";

export type EligibilityReason = "PROFILE_NOT_APPROVED" | "CITY" | "YEAR" | "BODY_TYPE" | "FULL";

export function checkEligibility(params: {
  driver: { status: string; city: string };
  vehicle: { year: number; bodyType: string };
  campaign: { cities: string; minCarYear: number; allowedBodyTypes: string; driverSlots: number };
  filledSlots: number;
}): EligibilityReason[] {
  const reasons: EligibilityReason[] = [];
  if (params.driver.status !== "APPROVED") reasons.push("PROFILE_NOT_APPROVED");
  if (!parseCities(params.campaign.cities).includes(params.driver.city as never)) reasons.push("CITY");
  if (params.vehicle.year < params.campaign.minCarYear) reasons.push("YEAR");
  let allowed: string[] = [];
  try {
    allowed = JSON.parse(params.campaign.allowedBodyTypes);
  } catch {
    allowed = [];
  }
  if (allowed.length > 0 && !allowed.includes(params.vehicle.bodyType)) reasons.push("BODY_TYPE");
  if (params.filledSlots >= params.campaign.driverSlots) reasons.push("FULL");
  return reasons;
}
