"""MAT-01, MAT-07 and the first half of the run (plan 04-03, task 1): constraint-first hybrid retrieval.

Everything is invented. Jobs get hand-made vectors and keywords, so the fusion is checked against numbers worked out by
hand, with no model in the way. Every test runs in the `conn` fixture's transaction, which is rolled back.
"""

import pytest
from sqlalchemy import text

from app.match import checker, retrieval
from app.platform.llm.embed import MODEL_ID
from tests.match.factories import (
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

QUERY_VECTOR = [1.0] + [0.0] * 383
MODES = ("hybrid", "keyword", "vector")


def vec(tilt):
    """A vector that points at QUERY_VECTOR when tilt is 0 and turns away from it as tilt grows."""
    return [1.0, tilt] + [0.0] * 382


def literal(vector):
    return "[" + ",".join(map(str, vector)) + "]"


@pytest.fixture
def person(conn):
    """An invented candidate whose record holds the factory's hard constraints: India, remote or hybrid."""
    user = make_user(conn)
    make_record(conn, user)
    return user


def add_job(conn, title, *, description=None, tilt=None, country="IN", arrangements=("remote",), **fields):
    """An open job with one requirement and, when a tilt is given, a vector of the current embedding model."""
    description = description or f"{title}. You will write python every day."
    job = make_job(
        conn,
        title=title,
        description=description,
        requirements=[("write python", "required")] if "write python" in description else [("every day", "required")],
        country=country,
        arrangements=arrangements,
        **fields,
    )
    if tilt is not None:
        conn.execute(
            text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
            {"v": literal(vec(tilt)), "m": MODEL_ID, "id": job.id},
        )
    return job


def ids(found):
    return [candidate.job_id for candidate in found]


# --- the filter comes first --------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_a_job_that_breaks_a_hard_constraint_is_never_returned_in_any_mode(conn, person, mode):
    inside = add_job(conn, "Python engineer", tilt=0.0)
    abroad = add_job(conn, "Python engineer abroad", tilt=0.1, country="DE")

    found = retrieval.candidates(conn, person, mode=mode, vector=QUERY_VECTOR, terms="python")

    assert abroad.id not in ids(found)
    assert ids(found) == [inside.id]


def test_a_job_that_does_not_state_its_country_is_returned_and_says_so(conn, person):
    unstated = add_job(conn, "Python engineer, place not stated", tilt=0.0, country=None)
    stated = add_job(conn, "Python engineer in India", tilt=0.5)

    found = {c.job_id: c for c in retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python")}

    assert found[unstated.id].unverified == ("location_not_stated",)
    assert found[stated.id].unverified == ()
    assert found[unstated.id].violated_kinds == ()


def test_only_open_canonical_listed_jobs_are_candidates(conn, person):
    open_job = add_job(conn, "Python engineer open", tilt=0.0)
    closed = add_job(conn, "Python engineer closed", tilt=0.1)
    folded = add_job(conn, "Python engineer duplicate", tilt=0.2)
    unlisted = add_job(conn, "Python engineer unlisted", tilt=0.3)
    conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id"), {"id": closed.id})
    conn.execute(text("UPDATE jobs SET canonical_job_id = :c WHERE id = :id"), {"id": folded.id, "c": open_job.id})
    conn.execute(text("UPDATE jobs SET is_listed = false WHERE id = :id"), {"id": unlisted.id})

    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python")) == [open_job.id]


def test_a_candidate_carries_the_fields_the_checker_reads(conn, person):
    job = add_job(
        conn,
        "Python engineer",
        tilt=0.0,
        city="Pune",
        employment_type="full_time",
        seniority="senior",
        salary_min=100,
        salary_max=200,
        salary_currency="INR",
        visa_sponsorship="offered",
    )

    (found,) = retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python")

    assert found.job_id == job.id and found.title == "Python engineer"
    assert found.facts == checker.JobFacts(
        company_name="Initech Demo",
        country="IN",
        city="Pune",
        work_arrangements=("remote",),
        employment_type="full_time",
        seniority="senior",
        salary_min=100,
        salary_max=200,
        salary_currency="INR",
        visa_sponsorship="offered",
    )
    assert found.content_version == 1 and found.requirements_version == 1 and found.change_note is None
    assert found.changed_at is not None


# --- a job is shown once per content version -----------------------------------------------------------------------


def test_a_job_already_shown_at_its_current_version_is_not_returned_but_is_at_a_newer_one(conn, person):
    job = add_job(conn, "Python engineer", tilt=0.0)
    other = add_job(conn, "Python engineer elsewhere", tilt=0.1)
    run = make_run(conn, person)
    facts = conn.execute(text("SELECT id FROM profile_facts WHERE user_id = :u ORDER BY id"), {"u": person}).scalars().all()
    make_shown_decision(conn, run, person, job, [Verdict("met", (facts[1],))])

    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python")) == [other.id]

    conn.execute(text("UPDATE jobs SET content_version = 2 WHERE id = :id"), {"id": job.id})
    assert set(ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python"))) == {job.id, other.id}


def test_a_job_that_was_only_withheld_is_still_a_candidate(conn, person):
    job = add_job(conn, "Python engineer", tilt=0.0)
    make_decision(conn, make_run(conn, person), person, job, outcome="withheld", reasons=("below_daily_cap",))

    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="python")) == [job.id]


# --- fusion --------------------------------------------------------------------------------------------------------


@pytest.fixture
def three(conn, person):
    """Vector order is a, b, c. Only b and c hold the word alpha, and b holds it in its title, so b leads the keywords."""
    a = add_job(conn, "Beta role", description="Beta role. Plain words, nothing to find. You will write python.", tilt=0.0)
    b = add_job(conn, "Alpha role", description="Alpha role. Alpha work. You will write python.", tilt=1.0)
    c = add_job(conn, "Gamma role", description="Gamma role. Mentions alpha once. You will write python.", tilt=5.0)
    return a.id, b.id, c.id


def test_two_lists_are_fused_by_reciprocal_rank_with_k_60(conn, person, three):
    a, b, c = three

    found = retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="alpha")

    # dense: a 1, b 2, c 3. sparse: b 1, c 2. A job's score is the sum of 1 / (60 + rank) over the lists it is in.
    assert ids(found) == [b, c, a]
    assert [round(x.rrf_score, 9) for x in found] == [
        round(1 / 62 + 1 / 61, 9),
        round(1 / 63 + 1 / 62, 9),
        round(1 / 61, 9),
    ]
    assert [x.retrieval_rank for x in found] == [1, 2, 3]


def test_the_result_is_limited_and_each_list_is_cut_at_its_depth(conn, person, three):
    a, b, c = three

    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="alpha", limit=2)) == [b, c]
    # depth 1 keeps a from the vectors and b from the keywords: both score 1/61, and the lower job id comes first
    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="alpha", depth=1)) == [a, b]


def test_each_mode_uses_only_its_own_list(conn, person, three):
    a, b, c = three

    assert ids(retrieval.candidates(conn, person, mode="keyword", vector=QUERY_VECTOR, terms="alpha")) == [b, c]
    assert ids(retrieval.candidates(conn, person, mode="vector", vector=QUERY_VECTOR, terms="alpha")) == [a, b, c]


def test_hybrid_falls_back_to_keywords_when_there_is_no_vector_or_no_job_has_one(conn, person, three):
    a, b, c = three

    assert ids(retrieval.candidates(conn, person, vector=None, terms="alpha")) == [b, c]  # the embedder is unavailable
    conn.execute(text("UPDATE jobs SET embedding = NULL, embedding_model = NULL"))
    assert ids(retrieval.candidates(conn, person, vector=QUERY_VECTOR, terms="alpha")) == [b, c]  # nothing is embedded


def test_vectors_of_another_embedding_model_are_ignored(conn, person, three):
    conn.execute(text("UPDATE jobs SET embedding_model = 'some-other-model'"))

    assert retrieval.candidates(conn, person, mode="vector", vector=QUERY_VECTOR) == []


def test_nothing_to_rank_on_returns_nothing(conn, person, three):
    assert retrieval.candidates(conn, person, vector=None, terms="") == []


def test_an_unknown_mode_is_refused(conn, person):
    with pytest.raises(ValueError):
        retrieval.candidates(conn, person, mode="semantic")


# --- the jobs the constraints removed ---------------------------------------------------------------------------------


def test_the_filtered_sample_is_the_twenty_best_ranked_violators_with_the_kinds_they_broke(conn, person):
    eligible = add_job(conn, "Python engineer", tilt=0.0)
    violators = [add_job(conn, f"Python engineer abroad {n:02d}", tilt=0.01 * (n + 1), country="DE") for n in range(25)]
    both = add_job(conn, "Python engineer on site abroad", tilt=0.001, country="DE", arrangements=("onsite",))
    for number, outside in enumerate(
        [
            ("closed", "UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id"),
            ("unlisted", "UPDATE jobs SET is_listed = false WHERE id = :id"),
        ]
    ):
        removed = add_job(conn, f"Python engineer abroad {outside[0]}", tilt=0.5, country="DE")
        conn.execute(text(outside[1]), {"id": removed.id})

    sample = retrieval.filtered_sample(conn, person, vector=QUERY_VECTOR, terms="")  # vectors alone: the order is theirs

    assert len(sample) == 20
    assert ids(sample) == [both.id, *[v.id for v in violators[:19]]]
    assert sample[0].violated_kinds == ("location", "remote_policy")
    assert all(c.violated_kinds == ("location",) for c in sample[1:])
    assert [c.retrieval_rank for c in sample] == list(range(1, 21))
    assert eligible.id not in ids(sample)
    assert retrieval.filtered_count(conn, person) == 26  # every open, listed, canonical violator, stored or not


def test_the_filtered_count_is_zero_when_nothing_breaks_a_constraint(conn, person):
    add_job(conn, "Python engineer", tilt=0.0)

    assert retrieval.filtered_count(conn, person) == 0
    assert retrieval.filtered_sample(conn, person, vector=QUERY_VECTOR, terms="python") == []


def test_a_person_with_no_constraints_has_no_violators(conn):
    free = make_user(conn)
    add_job(conn, "Python engineer abroad", tilt=0.0, country="DE")

    assert retrieval.filtered_count(conn, free) == 0
    assert ids(retrieval.candidates(conn, free, vector=QUERY_VECTOR, terms="python")) != []


# --- what is searched for ----------------------------------------------------------------------------------------


def fact(kind, statement):
    return {"kind": kind, "statement": statement}


def preference(kind, value, weight):
    return {"kind": kind, "value": value, "weight": weight}


def test_the_profile_passage_holds_the_facts_and_the_wanted_things_and_no_contact_detail():
    passage = retrieval.profile_text(
        [
            fact("role", "Senior data engineer at Initech Demo"),
            fact("skill", "Python"),
            fact("achievement", "Cut report time by 40 percent. Write to ada@example.com or +1 555 010 0147."),
            fact("other", "Portfolio https://example.com/ada and www.example.org/ada"),
        ],
        [preference("target_title", "staff engineer", 2), preference("industry", "fintech", -1)],
    )

    for kept in ("Senior data engineer at Initech Demo", "Python", "Cut report time by 40 percent", "staff engineer"):
        assert kept in passage
    assert "fintech" not in passage  # a thing the person does not want is not a thing to search for
    for contact in ("@", "http", "www.", "555", "example.com"):
        assert contact not in passage
    assert "[removed]" not in passage


def test_a_person_with_no_facts_has_an_empty_passage():
    assert retrieval.profile_text([], []) == ""


def test_the_keywords_are_or_joined_words_of_target_titles_then_skills():
    terms = retrieval.query_terms(
        [fact("skill", "Python"), fact("skill", "SQL (Postgres)"), fact("role", "Gardener")],
        [preference("target_title", "Staff Engineer", 2), preference("target_title", "sales", -2)],
    )

    assert terms == "staff or engineer or python or sql or postgres"  # no role fact, no title the person avoids


def test_the_keywords_never_carry_a_search_operator_and_stop_at_thirty():
    terms = retrieval.query_terms([fact("skill", '"R" or -C++ OR "x y"')], [])
    assert terms == "r or c or x or y"

    many = [fact("skill", f"skill{n}") for n in range(40)]
    assert retrieval.query_terms(many, []).split(" or ") == [f"skill{n}" for n in range(30)]


def test_no_skills_and_no_titles_give_no_keywords():
    assert retrieval.query_terms([fact("achievement", "Did things")], []) == ""


def test_the_active_preferences_are_read_without_the_superseded_ones(conn, person):
    event = conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', 'preference', 'create', '{}', 'candidate_edit', 2) RETURNING id"""
        ),
        {"u": person},
    ).scalar_one()
    old = conn.execute(
        text(
            """INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id)
               VALUES (:u, 'target_title', 'engineer', 1, 'An engineer.', 'candidate_edit', :e) RETURNING id"""
        ),
        {"u": person, "e": event},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id)
               VALUES (:u, 'target_title', 'staff engineer', 2, 'A staff engineer.', 'candidate_edit', :e)"""
        ),
        {"u": person, "e": event},
    )
    conn.execute(text("UPDATE preferences SET superseded_at = now() WHERE id = :id"), {"id": old})

    assert retrieval.active_preferences(conn, person) == [{"kind": "target_title", "value": "staff engineer", "weight": 2}]
