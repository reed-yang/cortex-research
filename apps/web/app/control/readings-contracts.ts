import { ContractDecodeError } from "./contracts";

export type ReadingsStatus = {
  enabled: boolean;
  failure: string | null;
  items: { source_id: string; state: "pending" | "publishing" | "published" | "failed" | "conflict" }[];
};

export function decodeReadingsStatus(value: unknown, path = "readings"): ReadingsStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractDecodeError(path, "expected publication status");
  }
  const row = value as Record<string, unknown>;
  if (typeof row.enabled !== "boolean" || !Array.isArray(row.items)) {
    throw new ContractDecodeError(path, "invalid publication status");
  }
  if (row.failure != null && typeof row.failure !== "string") {
    throw new ContractDecodeError(path, "invalid publication failure");
  }
  const states = ["pending", "publishing", "published", "failed", "conflict"];
  const items = row.items.map((item: unknown) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new ContractDecodeError(path, "invalid publication item");
    }
    const entry = item as Record<string, unknown>;
    if (typeof entry.source_id !== "string" || !entry.source_id || typeof entry.state !== "string" || !states.includes(entry.state)) {
      throw new ContractDecodeError(path, "invalid publication item");
    }
    return { source_id: entry.source_id, state: entry.state as ReadingsStatus["items"][number]["state"] };
  });
  if (new Set(items.map((item) => item.source_id)).size !== items.length) {
    throw new ContractDecodeError(path, "duplicate publication source");
  }
  return { enabled: row.enabled, failure: row.failure as string | null ?? null, items };
}
