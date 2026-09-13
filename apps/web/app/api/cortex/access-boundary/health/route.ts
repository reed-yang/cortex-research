import { NextRequest, NextResponse } from "next/server";
import { createAccessBoundaryAttestation } from "../../access-security";

export function GET(request: NextRequest) {
  const result = createAccessBoundaryAttestation(request);
  if ("allowed" in result) {
    const status = result.category === "access_boundary_unconfigured" ? 503 : 403;
    return NextResponse.json(
      {
        type: `urn:cortex:web-problem:${result.category}`,
        title: "The private access boundary could not be attested",
        status,
        category: result.category,
        retryable: status >= 500,
        owner: "cortex-web",
      },
      {
        status,
        headers: {
          "Cache-Control": "no-store",
          "Content-Type": "application/problem+json",
        },
      },
    );
  }

  return NextResponse.json(result, {
    status: 200,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json",
    },
  });
}
