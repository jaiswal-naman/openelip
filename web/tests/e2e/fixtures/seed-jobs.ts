import { execFileSync } from "node:child_process";
import path from "node:path";
import fixture from "./jobs-index.json";

/**
 * Puts the invented jobs of jobs-index.json into the stack's job index, so the browser tests have something to search,
 * page through and open. Nothing here is a real posting (see the file's own comment), and the boards are inactive, so
 * the worker never fetches them.
 *
 * It runs `psql` inside the stack's db container, the way phase1.spec.ts runs a command in the worker container, and it
 * signs in as the database's own superuser, because no API route writes jobs and the worker's login may not write
 * requirement rows by hand. COMPOSE_PROJECT_NAME in the environment picks the stack, as it does anywhere, and the seed
 * refuses to run unless it names a stack other than the default one, which holds the real index (see seedRefusal).
 *
 *   COMPOSE_PROJECT_NAME  the throwaway stack's Compose project; required, and never `openelip`
 *   E2E_DB_NAME           the database to seed, default `app`; a name ending in _test is a scratch one, no project needed
 *   E2E_COMPOSE_DIR       where `docker compose` runs, default the repository root
 *   E2E_SEED_JOBS         `always` to seed an index that holds real jobs too; the default refuses (see below)
 */
const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? path.resolve(__dirname, "../../../..");
const DB_NAME = process.env.E2E_DB_NAME ?? "app";
const DEFAULT_PROJECT = "openelip"; // the project of the stack that holds the real index, when Compose is run in the repository

const FIXTURE_BOARD_PREFIX = "e2e-fixture-"; // every invented board token starts with this
const REAL_INDEX_THRESHOLD = 1000; // an index with this many jobs from other boards is a real one, not a throwaway stack

type Ats = "greenhouse" | "lever" | "ashby";
type Pay = { min: number | null; max: number | null; currency: string };
type RequirementRow = { ordinal: number; importance: "required" | "preferred"; kind: string; text: string };

type FixtureJob = {
  ats: Ats;
  title: string;
  location_raw: string | null;
  country: string | null;
  city: string | null;
  work_arrangements: string[];
  employment_type: string;
  external_id?: string;
  description?: string;
  status?: "open" | "closed";
  is_listed?: boolean;
  closed_at?: string | null;
  last_verified_at?: string;
  pay?: Pay | null;
  requirements?: RequirementRow[];
};

type FixtureFile = {
  boards: Record<Ats, { board: string; company: string }>;
  last_verified_at: string;
  bulk_description: string;
  bulk: FixtureJob[];
  special: FixtureJob[];
};

/** One row of the seed: every field the SQL reads, with the defaults the fixture leaves out filled in. */
export type SeedJob = Required<FixtureJob> & { board: string; company: string; apply_url: string; posted_at: string | null };

const data = fixture as unknown as FixtureFile;

/** The bulk rows get a generic invented description and an id by position; the special jobs say everything themselves. */
export function expandJobs(): SeedJob[] {
  const rows = [
    ...data.bulk.map((row, index) => ({
      ...row,
      external_id: `inv-bulk-${String(index + 1).padStart(2, "0")}`,
      description: data.bulk_description.replace("{company}", data.boards[row.ats].company),
    })),
    ...data.special,
  ];
  return rows.map((row) => {
    const { board, company } = data.boards[row.ats];
    const external_id = row.external_id!;
    return {
      status: "open",
      is_listed: true,
      closed_at: null,
      last_verified_at: data.last_verified_at,
      pay: null,
      requirements: [],
      posted_at: null,
      apply_url: `https://jobs.example.test/${external_id}`,
      ...row,
      external_id,
      description: row.description!,
      board,
      company,
    };
  });
}

/**
 * One transaction: the boards, the jobs, their requirements, and the version stamp that says those requirements are for
 * the current text. Run again, it adds nothing and still lists the ids. A requirement's span is where its text first
 * appears in the description, found with strpos, which counts characters (code points) as the span trigger does, so an
 * emoji before a requirement is counted the way the database counts it. A text that is not in the description makes the
 * span invalid and the whole seed fails.
 */
export function buildSeedSql(jobs: SeedJob[] = expandJobs()): string {
  const json = JSON.stringify(jobs);
  if (json.includes("$seed$")) throw new Error("the fixture contains the SQL quote tag $seed$");
  return `
BEGIN;
CREATE TEMP TABLE seed ON COMMIT DROP AS SELECT $seed$${json}$seed$::jsonb AS doc;

INSERT INTO job_sources (ats, token, company_name, is_active)
SELECT DISTINCT (e.j->>'ats')::ats_type, e.j->>'board', e.j->>'company', false
  FROM seed, jsonb_array_elements(seed.doc) AS e(j)
ON CONFLICT (ats, token) DO NOTHING;

INSERT INTO jobs (source_id, external_id, title, company_name, location_raw, country, city, work_arrangements,
                  employment_type, description, apply_url, posted_at, content_hash, dedupe_key, status,
                  last_verified_at, closed_at, is_listed, salary_min, salary_max, salary_currency)
SELECT s.id, e.j->>'external_id', e.j->>'title', e.j->>'company', e.j->>'location_raw', e.j->>'country', e.j->>'city',
       ARRAY(SELECT jsonb_array_elements_text(e.j->'work_arrangements'))::work_arrangement[],
       (e.j->>'employment_type')::employment_type, e.j->>'description', e.j->>'apply_url',
       (e.j->>'posted_at')::timestamptz, md5(e.j->>'description'), 'e2e-fixture:' || (e.j->>'external_id'),
       (e.j->>'status')::job_status, (e.j->>'last_verified_at')::timestamptz, (e.j->>'closed_at')::timestamptz,
       (e.j->>'is_listed')::boolean,
       (e.j#>>'{pay,min}')::integer, (e.j#>>'{pay,max}')::integer, e.j#>>'{pay,currency}'
  FROM seed, jsonb_array_elements(seed.doc) AS e(j), job_sources s
 WHERE s.ats = (e.j->>'ats')::ats_type AND s.token = e.j->>'board'
ON CONFLICT (source_id, external_id) DO NOTHING;

INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span, prompt_version)
SELECT jb.id, jb.content_version, (q.r->>'ordinal')::smallint, (q.r->>'importance')::requirement_importance,
       (q.r->>'kind')::requirement_kind, q.r->>'text',
       int4range(strpos(jb.description, q.r->>'text') - 1,
                 strpos(jb.description, q.r->>'text') - 1 + char_length(q.r->>'text')),
       'e2e-fixture'
  FROM seed, jsonb_array_elements(seed.doc) AS e(j), jsonb_array_elements(e.j->'requirements') AS q(r), jobs jb
 WHERE jb.dedupe_key = 'e2e-fixture:' || (e.j->>'external_id')
ON CONFLICT (job_id, content_version, ordinal) DO NOTHING;

UPDATE jobs SET requirements_version = content_version
 WHERE dedupe_key IN (SELECT 'e2e-fixture:' || (e.j->>'external_id')
                        FROM seed, jsonb_array_elements(seed.doc) AS e(j)
                       WHERE jsonb_array_length(e.j->'requirements') > 0)
   AND requirements_version IS DISTINCT FROM content_version;

SELECT 'ID|' || substr(dedupe_key, length('e2e-fixture:') + 1) || '|' || id
  FROM jobs WHERE dedupe_key LIKE 'e2e-fixture:%' ORDER BY id;
COMMIT;
`;
}

function psql(sql: string): string {
  return execFileSync(
    "docker",
    ["compose", "exec", "-T", "db", "psql", "-U", "app", "-d", DB_NAME, "-v", "ON_ERROR_STOP=1", "-At", "-f", "-"],
    { cwd: COMPOSE_DIR, input: sql, encoding: "utf8" },
  );
}

/** The invented jobs' ids by external id (for example `inv-quokka-closed`), or null when the index is a real one and was left alone. */
export type SeededJobs = Record<string, number>;

/**
 * Why the seed must not run against this stack, or null when it may. It writes as the database's superuser into the
 * stack `docker compose` picks from COMPOSE_PROJECT_NAME, which defaults to `openelip`, the stack that holds the real
 * index. So it runs only when the project is named and is a different one, or when the target database is a scratch
 * one (its name ends in _test).
 */
export function seedRefusal(project: string | undefined, database: string): string | null {
  if (database.endsWith("_test")) return null;
  if (project && project !== DEFAULT_PROJECT) return null;
  return (
    `refusing to seed invented jobs into database "${database}" of the Compose project "${project || DEFAULT_PROJECT}", ` +
    `which may hold the real index. Start a throwaway stack under its own project name and set COMPOSE_PROJECT_NAME to ` +
    `it (and E2E_COMPOSE_DIR to its folder), or point E2E_DB_NAME at a database whose name ends in _test.`
  );
}

export function seedJobs(): SeededJobs | null {
  const refusal = seedRefusal(process.env.COMPOSE_PROJECT_NAME, DB_NAME);
  if (refusal) throw new Error(refusal);
  const counted = psql(
    `SELECT count(*) FILTER (WHERE s.token NOT LIKE '${FIXTURE_BOARD_PREFIX}%'), count(*) FILTER (WHERE s.token LIKE '${FIXTURE_BOARD_PREFIX}%')
       FROM jobs j JOIN job_sources s ON s.id = j.source_id;`,
  );
  const [others, ours] = counted.trim().split("|").map(Number);
  // Only a first seeding can add anything to someone's index. When the invented jobs are already there (an earlier spec
  // file put them in), carry on even if real jobs have arrived since: a fresh stack's worker may import boards meanwhile.
  if (ours === 0 && others >= REAL_INDEX_THRESHOLD && process.env.E2E_SEED_JOBS !== "always") return null;
  const ids = psql(buildSeedSql())
    .split("\n")
    .filter((line) => line.startsWith("ID|"))
    .map((line) => line.split("|"));
  return Object.fromEntries(ids.map(([, externalId, id]) => [externalId, Number(id)]));
}
