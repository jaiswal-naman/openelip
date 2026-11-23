import type { Metadata } from "next";
import { cache } from "react";
import { JobDetail } from "@/features/jobs/JobDetail";
import { loadJob } from "@/features/jobs/loadJob";
import { serverApi } from "@/lib/api/server";

type Props = { params: Promise<{ id: string }> };

// The page and its title both need the job; `cache` lets one request ask the API once for both.
const getJob = cache(async (id: string) => loadJob(await serverApi(), id));

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const job = await getJob((await params).id);
  return { title: `${job.title}, ${job.company_name}` };
}

/** One job, public (FRONTEND_UI 2.2): its fields, source and verified-open time, and its requirements as highlightable spans. */
export default async function JobPage({ params }: Props) {
  const job = await getJob((await params).id);
  return (
    <div className="container page">
      <JobDetail job={job} />
    </div>
  );
}
