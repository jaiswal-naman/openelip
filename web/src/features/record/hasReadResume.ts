import type { RecordView } from "./api";

/** Whether any resume on the record was read to the end: a document whose status is "succeeded". */
export const hasReadResume = (record: Pick<RecordView, "documents">): boolean => record.documents.some((document) => document.status === "succeeded");
