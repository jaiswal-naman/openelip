import { notFound } from "next/navigation";
import type { Api } from "@/lib/api/client";
import { isFailure, unwrap } from "@/lib/api/errors";
import type { JobDetail } from "./types";

// Job ids count up from 1. Fifteen digits is the most a JavaScript number holds exactly, far beyond any real index.
const JOB_ID = /^[1-9]\d{0,14}$/;

/**
 * The job for `/jobs/[id]`, for a Server Component. Anything that is not a job this page may show is the not-found page,
 * the same page for all of it so nothing is revealed (FRONTEND_UI 9.9):
 *   - an address that is not a whole-number id (threat T-02-37): not sent to the API at all;
 *   - an id the API does not know, or that is not a listed job: 404 (threat T-02-38);
 *   - an id the API refuses: 422.
 * Any other failure, such as the API being down, is thrown so the error page says so and offers Retry: a job that cannot
 * be read right now is not a job that does not exist.
 */
export async function loadJob(api: Api, id: string): Promise<JobDetail> {
  if (!JOB_ID.test(id)) notFound();
  try {
    return await unwrap(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: Number(id) } } }));
  } catch (error) {
    if (isFailure(error) && (error.status === 404 || error.status === 422)) notFound();
    throw error;
  }
}
