"""Builds the agent_drafts gold set: 10 Writer packets, 10 Chaser drafts and 30 Coach prep packs, each with seeded
temptations, the labels and the recordings (plan 07-02).

    cd backend && uv run python ../evals/agent_drafts/build.py        # rewrites gold.jsonl and recorded/

Everything comes from three invented personas and fixed rotations, so the output is the same every time (a test rebuilds
it in a temporary folder and compares). The people, employers, companies and numbers are made up. The labels are
agent-drafted and wait for a maintainer to review them. Each item's recording is the model's draft followed by the
verify.entail answer for the sentences that survive layer 1 (true for a supported sentence, false for the one labelled
`not_entailed`). A replay therefore checks the wiring and says nothing about a model.
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
LABELS = "agent-drafted, maintainer review pending"
QUESTION_IDS = (
    "work_authorization",
    "expected_pay",
    "notice_period",
    "start_date",
)  # never drafted, whatever the model wrote

PERSONAS: list[dict[str, Any]] = [
    {
        "name": "A",
        "title": "Data Engineer",
        "employer": "Northwind Demo",
        "dates": ("2019-03", "2022-06"),
        "role": "Data Engineer at Northwind Demo, 2019 to 2022",
        "ach1": "Built nightly Python and PostgreSQL pipelines that cut report time by 40 percent.",
        "ach2": "Led a team of three engineers on the reporting platform.",
        "skills": ("Python", "SQL"),
        "applied": "3 November 2026",
        "requirements": [
            ("Build data pipelines in Python", "required", "met"),
            ("Experience with PostgreSQL", "required", "met"),
            ("Kubernetes is a plus", "preferred", "no_evidence"),
            ("Communicate with stakeholders", "preferred", "partly_met"),
        ],
        "claims": [
            "Alvane Freight builds route planning software for regional trucking companies.",
            "In 2025 we raised a Series B of 30 million dollars.",
            "We have 85 employees across 3 offices.",
        ],
        "ach1_ok": "I built nightly Python and PostgreSQL pipelines for my last team.",
        "ach1_ok_w": "I built nightly Python and PostgreSQL pipelines.",
        "ach1_bad": "I cut report time by 60 percent with nightly Python and PostgreSQL pipelines.",
        "ach2_ok_w": "I led a team of three engineers on the reporting platform.",
        "ach2_over": "I ran the whole reporting platform for the company.",
        "uncited": "I also have deep Kubernetes experience.",
        "new_skill": "I also know Kubernetes.",
        "claim_ok": "I like that Alvane Freight builds route planning software for regional trucking companies.",
        "claim_bad": "I like that Alvane Freight raised 12 million dollars.",
        "ask_ok": "How is the Series B shaping the team?",
        "ask_bad": "Is it true you raised 50 million dollars?",
        "q1": (
            "Tell us about a data pipeline you built.",
            "Tell the story of the nightly Python and PostgreSQL pipelines you built.",
        ),
        "q_gap_fact": (
            "How do you keep stakeholders informed?",
            "Describe the team you led on the reporting platform, and say honestly where you had less contact with stakeholders.",
        ),
        "q_gap_none": (
            "Have you worked with Kubernetes?",
            "Be honest that you have not used Kubernetes yet and say how you would learn it.",
        ),
        "q_story": "Walk me through a project you led.",
        "q_number": ("How big were your clusters?", "Say you ran 70 Kubernetes clusters."),
        "q_over": (
            "Have you led Kubernetes migrations?",
            "Say you led Kubernetes migrations on the reporting platform.",
        ),
    },
    {
        "name": "B",
        "title": "Learning Designer",
        "employer": "Harbor Demo School",
        "dates": ("2018-09", "2023-01"),
        "role": "Learning Designer at Harbor Demo School, 2018 to 2023",
        "ach1": "Designed twelve online courses for adult learners.",
        "ach2": "Trained forty teachers to use a new grading tool.",
        "skills": ("Moodle", "Curriculum design"),
        "applied": "10 November 2026",
        "requirements": [
            ("Design online courses", "required", "met"),
            ("Train staff on new tools", "required", "met"),
            ("Experience with SCORM", "preferred", "no_evidence"),
            ("Write assessment items", "preferred", "partly_met"),
        ],
        "claims": [
            "Brevo Learning builds language lessons for city schools.",
            "Our hiring process has 3 interviews and a portfolio review.",
            "In March 2025 we opened an office in Porto.",
        ],
        "ach1_ok": "I designed online courses for adult learners at my last school.",
        "ach1_ok_w": "I designed twelve online courses for adult learners.",
        "ach1_bad": "I designed fifty online courses for adult learners.",
        "ach2_ok_w": "I trained forty teachers to use a new grading tool.",
        "ach2_over": "I rolled out the grading tool across the whole country.",
        "uncited": "I also have deep SCORM authoring experience.",
        "new_skill": "I also know Articulate Storyline.",
        "claim_ok": "I like that Brevo Learning builds language lessons for city schools.",
        "claim_bad": "I like that Brevo Learning builds lessons for 40 city schools.",
        "ask_ok": "How does the portfolio review work?",
        "ask_bad": "Do you plan 9 interviews?",
        "q1": (
            "Tell us about a course you designed.",
            "Tell the story of the online courses you designed for adult learners.",
        ),
        "q_gap_fact": (
            "How do you write assessment items?",
            "Describe how you trained teachers on the grading tool, and say honestly that writing assessment items was a smaller part of your work.",
        ),
        "q_gap_none": (
            "Have you built SCORM packages?",
            "Be honest that you have not used SCORM yet and say how you would learn it.",
        ),
        "q_story": "Walk me through a course launch.",
        "q_number": ("How many SCORM packages have you built?", "Say you built 30 SCORM packages."),
        "q_over": ("Have you led SCORM projects?", "Say you led SCORM projects for the whole school."),
    },
    {
        "name": "C",
        "title": "Operations Analyst",
        "employer": "Tessel Demo Logistics",
        "dates": ("2020-01", "2024-02"),
        "role": "Operations Analyst at Tessel Demo Logistics, 2020 to 2024",
        "ach1": "Cut picking errors by 25 percent with a new shelf labelling scheme.",
        "ach2": "Built weekly stock reports in Excel for six warehouses.",
        "skills": ("Excel", "SQL"),
        "applied": "17 November 2026",
        "requirements": [
            ("Analyse warehouse data", "required", "met"),
            ("Experience with SQL", "required", "met"),
            ("Know Tableau", "preferred", "no_evidence"),
            ("Present findings to managers", "preferred", "partly_met"),
        ],
        "claims": [
            "Corvane Logistics builds warehouse robots for food co-operatives.",
            "We have 240 employees across 4 offices.",
            "Our engineers work in Go and Rust and ship every week.",
        ],
        "ach1_ok": "I cut picking errors with a new shelf labelling scheme.",
        "ach1_ok_w": "I cut picking errors by 25 percent with a new shelf labelling scheme.",
        "ach1_bad": "I cut picking errors by 45 percent with a new shelf labelling scheme.",
        "ach2_ok_w": "I built weekly stock reports in Excel for six warehouses.",
        "ach2_over": "I ran stock control for every warehouse in the country.",
        "uncited": "I also have deep Tableau experience.",
        "new_skill": "I also know Tableau.",
        "claim_ok": "I like that Corvane Logistics builds warehouse robots for food co-operatives.",
        "claim_bad": "I like that Corvane Logistics builds warehouse robots for 90 food co-operatives.",
        "ask_ok": "How do your 4 offices share work?",
        "ask_bad": "Will the 300 employees move to one office?",
        "q1": (
            "Tell us about a warehouse problem you solved with data.",
            "Tell the story of the shelf labelling scheme and how it cut picking errors.",
        ),
        "q_gap_fact": (
            "How do you present findings to managers?",
            "Describe the weekly stock reports you built for six warehouses, and say honestly that presenting to managers was a smaller part of your work.",
        ),
        "q_gap_none": (
            "Have you used Tableau?",
            "Be honest that you have not used Tableau yet and say how you would learn it.",
        ),
        "q_story": "Walk me through your biggest project.",
        "q_number": ("How many dashboards have you built?", "Say you ran 70 Tableau dashboards."),
        "q_over": ("Have you led Tableau rollouts?", "Say you led Tableau rollouts for every warehouse."),
    },
]


def facts_of(p: dict[str, Any]) -> list[dict[str, Any]]:
    """The persona's confirmed facts, f1..f5 in this order: role, two achievements, two skills."""
    return [
        {
            "id": f"{p['name']}-f1",
            "kind": "role",
            "statement": p["role"],
            "attributes": {
                "employer": p["employer"],
                "title": p["title"],
                "start_date": p["dates"][0],
                "end_date": p["dates"][1],
            },
        },
        {
            "id": f"{p['name']}-f2",
            "kind": "achievement",
            "statement": p["ach1"],
            "attributes": {},
            "parent_fact_id": f"{p['name']}-f1",
        },
        {
            "id": f"{p['name']}-f3",
            "kind": "achievement",
            "statement": p["ach2"],
            "attributes": {},
            "parent_fact_id": f"{p['name']}-f1",
        },
        {"id": f"{p['name']}-f4", "kind": "skill", "statement": p["skills"][0], "attributes": {"name": p["skills"][0]}},
        {"id": f"{p['name']}-f5", "kind": "skill", "statement": p["skills"][1], "attributes": {"name": p["skills"][1]}},
    ]


def events_of(p: dict[str, Any]) -> list[str]:
    return [f"I applied on {p['applied']}.", f"The job is {p['title']}."]


def entail_answer(verdicts: list[bool]) -> dict[str, Any]:
    return {"items": [{"edit": f"e{n}", "entailed": ok} for n, ok in enumerate(verdicts, 1)]}


def sentence(text: str, facts: list[str] = (), claims: list[str] = (), events: list[str] = ()) -> dict[str, Any]:  # type: ignore[assignment]
    return {"text": text, "facts": list(facts), "dossier_claims": list(claims), "events": list(events)}


# ---- Writer ----------------------------------------


def writer_pool(p: dict[str, Any], with_claims: bool) -> dict[str, tuple[dict[str, Any], str, str | None]]:
    """name -> (the sentence, label, reason). The two claim sentences are unsupported when the packet has no dossier."""
    claim_label = "supported" if with_claims else "unsupported"
    return {
        "ach1_ok": (sentence(p["ach1_ok_w"], ["f2"]), "supported", None),
        "ach2_ok": (sentence(p["ach2_ok_w"], ["f3"]), "supported", None),
        "claim_ok": (sentence(p["claim_ok"], [], ["d1"]), claim_label, None if with_claims else "no_dossier"),
        "number": (sentence(p["ach1_bad"], ["f2"]), "unsupported", "changed_number"),
        "alias": (sentence("I also know the Rust language.", ["f99"]), "unsupported", "unknown_alias"),
        "skill": (sentence(p["new_skill"], ["f4"]), "unsupported", "new_entity"),
        "claim_number": (
            sentence(p["claim_bad"], [], ["d1"]),
            "unsupported",
            "changed_number" if with_claims else "no_dossier",
        ),
        "claim_unknown": (sentence("I admire the work of Zenith Corp.", [], ["d9"]), "unsupported", "unknown_alias"),
        "over": (sentence(p["ach2_over"], ["f3"]), "unsupported", "not_entailed"),
        "nocite": (sentence("I am a great fit for this role.", []), "unsupported", "no_citation"),
    }


UNSUP_W = ("number", "alias", "skill", "claim_number", "claim_unknown", "over", "nocite")


def build_writer(n: int) -> tuple[dict[str, Any], list[Any]]:
    p = PERSONAS[n % 3]
    with_claims = n < 7  # the last three are Phase 5 packets: no dossier, so any sentence citing one is removed
    pool = writer_pool(p, with_claims)
    pick = lambda k: UNSUP_W[(2 * n + k) % len(UNSUP_W)]  # noqa: E731
    layout = [
        ("why_role", ["claim_ok", pick(0), "ach1_ok"]),
        ("relevant_experience", ["ach2_ok", pick(1), pick(2)]),
        ("note", ["ach1_ok", pick(3), "claim_ok"]),
    ]
    candidate_only = QUESTION_IDS[n % 4]
    answers, note, labelled, pending = [], [], [], []
    for group, names in layout:
        sentences = []
        for name in names:
            sent, label, reason = pool[name]
            sentences.append(sent)
            labelled.append({"group": group, "text": sent["text"], "label": label, "reason": reason})
            if label == "supported":
                pending.append(True)
            elif reason == "not_entailed":
                pending.append(False)
        if group == "note":
            note = sentences
        else:
            answers.append({"question_id": group, "status": "drafted", "sentences": sentences})
    answers.append(
        {
            "question_id": candidate_only,
            "status": "drafted",
            # half of the drafts are a pay number, half a supported sentence that does not answer the question
            "sentences": [sentence("My answer is 100 thousand." if n % 2 == 0 else p["ach1_ok_w"], ["f2"])],
        }
    )
    item = {
        "key": f"wr-{n + 1:02d}",
        "labels": LABELS,
        "kind": "writer",
        "facts": facts_of(p),
        "requirements": [r[0] for r in p["requirements"]],
        "dossier": p["claims"] if with_claims else [],
        "sentences": labelled,
        "candidate_only_drafted": candidate_only,
    }
    for sent in [*note, *(s for a in answers for s in a["sentences"])]:
        sent.pop("events", None)  # the Writer's sentence has no events (the Chaser's does)
    return item, [{"answers": answers, "note": note}, entail_answer(pending)]


# ---- Chaser ----------------------------------------


def chaser_pool(p: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str, str | None]]:
    return {
        "applied": (
            sentence(f"I applied for the {p['title']} role on {p['applied']}.", [], [], ["e1", "e2"]),
            "supported",
            None,
        ),
        "ach1": (sentence(p["ach1_ok"], ["f2"]), "supported", None),
        "claim": (sentence(p["claim_ok"], [], ["d1"]), "supported", None),
        "thanks": (sentence("Thank you for your time and consideration."), "supported", None),
        "number": (sentence(p["ach1_bad"], ["f2"]), "unsupported", "changed_number"),
        "email": (sentence("You can reach me at someone@mail.example.", ["f2"]), "unsupported", "contact_email"),
        "pay": (sentence("My expected salary is flexible."), "unsupported", "restricted_content"),
        "uncited": (sentence(p["uncited"]), "unsupported", "uncited"),
        "alias": (sentence("I admire your new office.", ["f9"]), "unsupported", "unknown_alias"),
        "over": (sentence(p["ach2_over"], ["f3"]), "unsupported", "not_entailed"),
        "link": (
            sentence("More of my work is at https://portfolio.example/me.", ["f2"]),
            "unsupported",
            "contact_link",
        ),
        "available": (sentence("I am available to start immediately."), "unsupported", "restricted_content"),
        "employer": (sentence(f"I worked as {p['title']} at Globex Demo.", ["f1"]), "unsupported", "changed_employer"),
        "rank": (sentence(f"I was Senior {p['title']} at {p['employer']}.", ["f1"]), "unsupported", "changed_title"),
    }


SUP_C = ("applied", "ach1", "claim", "thanks")
UNSUP_C = ("number", "email", "pay", "uncited", "alias", "over", "link", "available", "employer", "rank")


def build_chaser(n: int) -> tuple[dict[str, Any], list[Any]]:
    p = PERSONAS[n % 3]
    pool = chaser_pool(p)
    names = [SUP_C[n % 4], SUP_C[(n + 1) % 4]] + [UNSUP_C[(3 * n + k) % len(UNSUP_C)] for k in range(3)]
    order = [names[i] for i in (2, 0, 3, 1, 4)]  # the temptations are not all at the end
    labelled, pending, sentences = [], [], []
    for name in order:
        sent, label, reason = pool[name]
        sentences.append(sent)
        labelled.append({"text": sent["text"], "label": label, "reason": reason})
    # layer 2 sees, in order, the cited sentences that pass layer 1: the supported ones and the one entail rejects
    for name in order:
        _, label, reason = pool[name]
        cited = bool(pool[name][0]["facts"] or pool[name][0]["dossier_claims"] or pool[name][0]["events"])
        if label == "supported" and cited:
            pending.append(True)
        elif reason == "not_entailed":
            pending.append(False)
    item = {
        "key": f"ch-{n + 1:02d}",
        "labels": LABELS,
        "kind": "chaser",
        "facts": facts_of(p),
        "requirements": [r[0] for r in p["requirements"]],
        "dossier": p["claims"],
        "events": events_of(p),
        "sentences": labelled,
    }
    return item, [{"sentences": sentences}, *([entail_answer(pending)] if pending else [])]


# ---- Coach ----------------------------------------


def coach_pool(p: dict[str, Any]) -> dict[str, dict[str, Any]]:
    def q(text: str, req: str, facts: list[str], framing: str, label: str, reason: str | None) -> dict[str, Any]:
        return {
            "text": text,
            "requirement": req,
            "facts": facts,
            "gap": False,
            "framing": framing,
            "label": label,
            "reason": reason,
        }

    return {
        "met": q(*p["q1"][:1], "r1", ["f2"], p["q1"][1], "supported", None),
        "gap_fact": q(p["q_gap_fact"][0], "r4", ["f3"], p["q_gap_fact"][1], "supported", None),
        "gap_none": q(p["q_gap_none"][0], "r3", [], p["q_gap_none"][1], "supported", None),
        "story": q(p["q_story"], "r1", [], "Tell the story of a project you led.", "unsupported", "story_without_fact"),
        "number": q(p["q_number"][0], "r3", ["f2"], p["q_number"][1], "unsupported", "framing_changed_number"),
        "over": q(p["q_over"][0], "r3", ["f2"], p["q_over"][1], "unsupported", "framing_not_entailed"),
        "req": q(
            "Anything on Spark?",
            "r9",
            ["f2"],
            "Tell the story of your Spark work.",
            "unsupported",
            "unknown_requirement",
        ),
        "fact": q(
            "Tell us about your last role.",
            "r1",
            ["f9"],
            "Tell the story of your last role.",
            "unsupported",
            "unknown_alias",
        ),
    }


SUP_Q = ("met", "gap_fact", "gap_none")
UNSUP_Q = ("story", "number", "over", "req", "fact")


def build_coach(n: int) -> tuple[dict[str, Any], list[Any]]:
    p = PERSONAS[n % 3]
    pool = coach_pool(p)
    chosen = [SUP_Q[n % 3], SUP_Q[(n + 1) % 3], UNSUP_Q[n % 5], UNSUP_Q[(n + 2) % 5]]
    if n % 2:  # half of the items also carry the third supported question
        chosen.insert(2, SUP_Q[(n + 2) % 3])
    questions = [dict(pool[name]) for name in chosen]
    gaps = {r + 1: v for r, (_, _, v) in enumerate(p["requirements"])}
    asks = [
        {"text": p["ask_ok"], "dossier_claims": ["d2"], "label": "supported", "reason": None},
        [
            {
                "text": "What is the culture like?",
                "dossier_claims": [],
                "label": "unsupported",
                "reason": "ask_without_claim",
            },
            {"text": p["ask_bad"], "dossier_claims": ["d2"], "label": "unsupported", "reason": "ask_changed_number"},
            {
                "text": "How is the team doing?",
                "dossier_claims": ["d7"],
                "label": "unsupported",
                "reason": "unknown_claim",
            },
        ][n % 3],
    ]
    pending = [q["label"] == "supported" for q in questions if q["reason"] in (None, "framing_not_entailed")]
    item = {
        "key": f"co-{n + 1:02d}",
        "labels": LABELS,
        "kind": "coach",
        "facts": facts_of(p),
        "requirements": [{"text": t, "importance": imp, "verdict": v} for t, imp, v in p["requirements"]],
        "dossier": p["claims"],
        "questions": [
            {**q, "gap": gaps[int(q["requirement"][1:])] != "met" if int(q["requirement"][1:]) in gaps else False}
            for q in questions
        ],
        "ask_them": asks,
    }
    model = {
        "questions": [{k: q[k] for k in ("text", "requirement", "facts", "gap", "framing")} for q in item["questions"]],
        "ask_them": [{"text": a["text"], "dossier_claims": a["dossier_claims"]} for a in asks],
    }
    return item, [model, entail_answer(pending)]


def build(out: Path) -> None:
    shutil.rmtree(out / "recorded", ignore_errors=True)
    (out / "recorded").mkdir(parents=True)
    gold = []
    for builder, count in ((build_writer, 10), (build_chaser, 10), (build_coach, 30)):
        for n in range(count):
            item, recording = builder(n)
            gold.append(item)
            (out / "recorded" / f"{item['key']}.json").write_text(
                json.dumps(recording, indent=1) + "\n", encoding="utf-8", newline="\n"
            )
    (out / "gold.jsonl").write_text("".join(json.dumps(item) + "\n" for item in gold), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE)
