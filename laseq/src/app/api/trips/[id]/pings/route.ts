import { NextResponse } from "next/server";
import { z } from "zod";
import { requireDriverApi } from "../../../_auth";
import { addPings } from "@/lib/trips";

const body = z.object({
  pings: z
    .array(
      z.object({
        lat: z.number().min(-90).max(90),
        lng: z.number().min(-180).max(180),
        speedKmh: z.number().min(0).max(400).nullable().optional(),
        recordedAt: z.union([z.string(), z.number()]).optional(),
      }),
    )
    .min(1)
    .max(500),
});

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const driver = await requireDriverApi();
  if (!driver) return NextResponse.json({ error: "UNAUTHORIZED" }, { status: 401 });
  const { id } = await ctx.params;
  const parsed = body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) return NextResponse.json({ error: "BAD_REQUEST" }, { status: 400 });
  const res = await addPings(id, driver.id, parsed.data.pings);
  if (!res.ok) return NextResponse.json({ error: res.error }, { status: 404 });
  return NextResponse.json({ accepted: res.count });
}
