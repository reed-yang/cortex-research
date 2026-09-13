import { CortexPrototype } from "./cortex-prototype";
import { Shell } from "./shell/shell";
import { notFound } from "next/navigation";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ capture?: string; mode?: string; scenario?: string }>;
}) {
  const params = await searchParams;
  if (params.mode !== undefined && params.mode !== "demo" && params.mode !== "control") {
    notFound();
  }
  if (params.scenario !== undefined && params.scenario !== "g0" && params.scenario !== "g1") {
    notFound();
  }
  if (params.mode !== "demo" && (params.capture !== undefined || params.scenario !== undefined)) {
    notFound();
  }
  const scenario = params.scenario === "g1" ? "g1" : "g0";

  return params.mode === "demo" ? (
    <CortexPrototype
      captureMode={params.capture === "full"}
      initialScenario={scenario}
    />
  ) : <Shell />;
}
