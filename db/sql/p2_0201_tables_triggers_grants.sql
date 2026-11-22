-- Revision p2_0201_job_index, part 2: the skills and requirement tables, the guards that keep requirement spans true,
-- append-only history and the grants. Read by db/versions/p2_0201_job_index.py, which runs it as app_owner, so every
-- object below is owned by app_owner and every grant is issued by its owner (ARCHITECTURE 7.1).
--
-- Not here on purpose: foreign keys to organizations, profile_facts and preferences, and the app_match grants. Those
-- tables and that role belong to other phases, which add their own constraints.

-- DATA_MODEL 3.5. Skills taxonomy (reference data). It starts empty: no document names a taxonomy source yet.
CREATE TABLE skills (
  id               integer PRIMARY KEY,
  external_id      text    NOT NULL UNIQUE,
  name             text    NOT NULL,
  category         text,
  aliases          text[]  NOT NULL DEFAULT '{}',
  taxonomy_version text    NOT NULL
);

-- Requirements, extracted once per content version (IDX-08). The text is verbatim from the posting.
CREATE TABLE job_requirements (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id          bigint                 NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  content_version integer                NOT NULL,
  ordinal         smallint               NOT NULL,
  importance      requirement_importance NOT NULL,
  kind            requirement_kind       NOT NULL,
  text            text                   NOT NULL,   -- equals jobs.description at span, checked on insert
  span            int4range              NOT NULL,   -- half-open character range in jobs.description
  skill_ids       integer[]              NOT NULL DEFAULT '{}',
  min_years       smallint               CHECK (min_years BETWEEN 0 AND 60),  -- only when the text states a number
  prompt_version  text                   NOT NULL,
  llm_call_id     bigint,
  created_at      timestamptz            NOT NULL DEFAULT now(),
  UNIQUE (job_id, content_version, ordinal),
  CHECK (NOT isempty(span) AND lower(span) >= 0 AND NOT upper_inf(span))
);

-- DATA_MODEL 3.14: the cost-ledger row that produced a requirement; the row survives the ledger row.
ALTER TABLE job_requirements ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL;

-- DATA_MODEL 4.2, verbatim. A requirement is accepted only if its text is the job description at its span.
-- substr counts characters, so a span is in Unicode code points, the same unit Python string indexes use.
CREATE FUNCTION job_requirements_check_span() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM 1 FROM jobs j
  WHERE j.id = NEW.job_id
    AND j.content_version = NEW.content_version
    AND substr(j.description, lower(NEW.span) + 1, upper(NEW.span) - lower(NEW.span)) = NEW.text;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'span_unresolved: requirement text is not the current job description at its span'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER job_requirements_span BEFORE INSERT ON job_requirements
  FOR EACH ROW EXECUTE FUNCTION job_requirements_check_span();

-- Spans stay valid because the text of a version never changes: new text means a new content_version.
CREATE FUNCTION jobs_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.content_version < OLD.content_version THEN
    RAISE EXCEPTION 'jobs.content_version cannot go down' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.title, NEW.description) IS DISTINCT FROM (OLD.title, OLD.description)
     AND NEW.content_version = OLD.content_version THEN
    RAISE EXCEPTION 'jobs: new text needs a new content_version' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER jobs_version_guard BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION jobs_version_guard();

-- DATA_MODEL 4.5. Append-only: forbid_mutation comes from revision 0002_platform.
CREATE TRIGGER fetch_runs_append_only       BEFORE UPDATE OR DELETE ON fetch_runs       FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_requirements_append_only BEFORE UPDATE OR DELETE ON job_requirements FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- DATA_MODEL 5.7, for the roles that exist today. The API only reads; the worker writes requirements and the taxonomy.
GRANT SELECT ON job_requirements, skills TO app_api;
GRANT SELECT, INSERT ON job_requirements TO app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON skills TO app_worker;
