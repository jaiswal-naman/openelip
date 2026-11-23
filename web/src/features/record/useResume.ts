import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api/context";
import { documentQuery, recordQuery } from "./api";

/**
 * The resume the facts on screen were read from: the one named, otherwise the newest that was read to the end. It comes
 * with its numbered lines, which say which line each fact's words are on. `latest` is null when no resume was read.
 */
export function useResume(documentId: string | null = null) {
  const api = useApi();
  const record = useQuery(recordQuery(api));
  const latest = documentId ?? record.data?.documents.find((document) => document.status === "succeeded")?.id ?? null;
  const resume = useQuery({ ...documentQuery(api, latest ?? "", true), enabled: latest !== null });
  return { record, latest, resume };
}
