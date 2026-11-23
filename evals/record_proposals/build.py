"""The authored source of the record_proposals gold set (plan 05-06): 20 invented candidate messages, the recorded
`record.propose` answer to each and the proposals a careful reader expects. `python evals/record_proposals/build.py`
(from backend/, with `uv run`) writes gold.jsonl, recordings/ and manifest.json from it; the suite's test fails if the
committed files differ from what this writes.

The record each script starts from is person A of evals/chat/seed.py: three confirmed facts, India only, remote or hybrid
only, never Globex Demo, a target-title preference "data engineer". A script may add facts (`facts`) or a proposal the
candidate declined some days ago (`declined`). `expect` lists what should be stored: entity, operation (create, update or
remove: the record's words) and the part of the proposed row that must match. An empty list means: store nothing.
Everything is invented and labelled agent-drafted, maintainer review pending.
"""

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
LABELS = "agent-drafted, maintainer review pending"


def item(
    entity: str, field: str, value: dict[str, Any], quote: str, statement: str, operation: str = "set"
) -> dict[str, Any]:
    return {
        "entity": entity,
        "operation": operation,
        "field": field,
        "value": value,
        "evidence_quote": quote,
        "statement": statement,
    }


def answer(*items: dict[str, Any]) -> dict[str, Any]:
    return {"proposals": list(items)}


PUNE = item(
    "preference", "location", {"text": "Pune", "weight": 2}, "I would also take jobs in Pune", "Open to jobs in Pune."
)

# key, kind of case, message, setup, the recorded answer, what should be stored
SCRIPTS: list[dict[str, Any]] = [
    {
        "key": "rp-01",
        "case": "preference",
        "message": "I would also take jobs in Pune.",
        "answer": answer(PUNE),
        "expect": [{"entity": "preference", "operation": "create", "after": {"kind": "location", "value": "Pune"}}],
    },
    {
        "key": "rp-02",
        "case": "skill",
        "message": "I also know Terraform well.",
        "answer": answer(item("fact", "skill", {"text": "Terraform"}, "I also know Terraform", "Knows Terraform.")),
        "expect": [{"entity": "fact", "operation": "create", "after": {"kind": "skill", "statement": "Terraform"}}],
    },
    {
        "key": "rp-03",
        "case": "certification",
        "message": "I hold the Demo Cloud Practitioner certificate.",
        "answer": answer(
            item(
                "fact",
                "certification",
                {"text": "Demo Cloud Practitioner certificate"},
                "I hold the Demo Cloud Practitioner certificate",
                "Holds the Demo Cloud Practitioner certificate.",
            )
        ),
        "expect": [{"entity": "fact", "operation": "create", "after": {"kind": "certification"}}],
    },
    {
        "key": "rp-04",
        "case": "constraint",
        "message": "I will not take anything under 2,000,000 INR a year.",
        "answer": answer(
            item(
                "constraint",
                "salary_floor",
                {"salary_floor": {"amount": 2000000, "currency": "INR"}},
                "I will not take anything under 2,000,000 INR a year",
                "At least 2,000,000 INR a year.",
            )
        ),
        "expect": [{"entity": "constraint", "operation": "create", "after": {"kind": "salary_floor"}}],
    },
    {
        "key": "rp-05",
        "case": "constraint update",
        "message": "Please never show me jobs at Hooli Demo.",
        "answer": answer(
            item(
                "constraint",
                "excluded_company",
                {"company_names": ["Globex Demo", "Hooli Demo"]},
                "never show me jobs at Hooli Demo",
                "Never Globex Demo or Hooli Demo.",
            )
        ),
        "expect": [
            {
                "entity": "constraint",
                "operation": "update",
                "after": {"kind": "excluded_company", "company_names": ["Globex Demo", "Hooli Demo"]},
            }
        ],
    },
    {
        "key": "rp-06",
        "case": "constraint update",
        "message": "I only want remote work now.",
        "answer": answer(
            item(
                "constraint",
                "remote_policy",
                {"arrangements": ["remote"]},
                "I only want remote work now",
                "Remote work only.",
            )
        ),
        "expect": [
            {
                "entity": "constraint",
                "operation": "update",
                "after": {"kind": "remote_policy", "arrangements": ["remote"]},
            }
        ],
    },
    {
        "key": "rp-07",
        "case": "two proposals",
        "message": "I also know Kubernetes, and I would consider Mumbai.",
        "answer": answer(
            item("fact", "skill", {"text": "Kubernetes"}, "I also know Kubernetes", "Knows Kubernetes."),
            item(
                "preference",
                "location",
                {"text": "Mumbai", "weight": 1},
                "I would consider Mumbai",
                "Would consider Mumbai.",
            ),
        ),
        "expect": [
            {"entity": "fact", "operation": "create", "after": {"kind": "skill", "statement": "Kubernetes"}},
            {"entity": "preference", "operation": "create", "after": {"kind": "location", "value": "Mumbai"}},
        ],
    },
    {
        "key": "rp-08",
        "case": "dealbreaker",
        "message": "No on-call rotations for me, ever.",
        "answer": answer(
            item(
                "preference",
                "untyped_dealbreaker",
                {"text": "on-call rotations"},
                "No on-call rotations for me, ever",
                "No on-call rotations.",
            )
        ),
        "expect": [
            {"entity": "preference", "operation": "create", "after": {"kind": "untyped_dealbreaker", "weight": -3}}
        ],
    },
    {
        "key": "rp-09",
        "case": "removal",
        "message": "Forget that I wanted data engineer roles.",
        "answer": answer(
            item(
                "preference",
                "target_title",
                {"text": "data engineer"},
                "Forget that I wanted data engineer roles",
                "Remove the data engineer target.",
                operation="remove",
            )
        ),
        "expect": [{"entity": "preference", "operation": "remove", "after": None}],
    },
    {
        "key": "rp-10",
        "case": "trap: quote taken from a job posting",
        "message": "What does the Globex posting say about the stack?",
        "answer": answer(
            item(
                "fact",
                "skill",
                {"text": "Kubernetes"},
                "Experience with Kubernetes is required",
                "Knows Kubernetes.",
            )
        ),
        "expect": [],
    },
    {
        "key": "rp-11",
        "case": "trap: protected characteristic",
        "message": "I am a single mother, so I would like part-time work.",
        "answer": answer(
            item(
                "preference",
                "other",
                {"text": "part-time because single mother", "weight": 2},
                "I am a single mother",
                "Single mother who wants part-time work.",
            )
        ),
        "expect": [],
    },
    {
        "key": "rp-12",
        "case": "trap: equal to what the record holds",
        "message": "I only want jobs in India.",
        "answer": answer(
            item(
                "constraint",
                "location",
                {"country_codes": ["IN"]},
                "I only want jobs in India",
                "Only jobs in India.",
            )
        ),
        "expect": [],
    },
    {
        "key": "rp-13",
        "case": "trap: declined ten days ago",
        "message": "I would also take jobs in Pune.",
        "declined": [
            {
                "entity": "preference",
                "operation": "create",
                "after": {"kind": "location", "statement": "Open to jobs in Pune.", "value": "Pune", "weight": 2},
                "days": 10,
            }
        ],
        "answer": answer(PUNE),
        "expect": [],
    },
    {
        "key": "rp-14",
        "case": "trap: a fact of kind role",
        "message": "I worked at Hooli Demo as a staff engineer.",
        "answer": answer(
            item(
                "fact",
                "role",
                {"text": "Staff engineer at Hooli Demo"},
                "I worked at Hooli Demo as a staff engineer",
                "Staff engineer at Hooli Demo.",
            )
        ),
        "expect": [],
    },
    {
        "key": "rp-15",
        "case": "trap: a quote that is a paraphrase",
        "message": "I am good with Rust.",
        "answer": answer(item("fact", "skill", {"text": "Rust"}, "Proficient in Rust", "Proficient in Rust.")),
        "expect": [],
    },
    {
        "key": "rp-16",
        "case": "trap: a skill the record already holds",
        "message": "I know Python.",
        "facts": [["skill", "Python"]],
        "answer": answer(item("fact", "skill", {"text": "Python"}, "I know Python", "Knows Python.")),
        "expect": [],
    },
    {
        "key": "rp-17",
        "case": "trap: one good item beside a protected one",
        "message": "I know Go. Also, my religion is Buddhist.",
        "answer": answer(
            item("fact", "skill", {"text": "Go"}, "I know Go", "Knows Go."),
            item(
                "preference",
                "other",
                {"text": "Buddhist holidays", "weight": 1},
                "my religion is Buddhist",
                "Buddhist.",
            ),
        ),
        "expect": [{"entity": "fact", "operation": "create", "after": {"kind": "skill", "statement": "Go"}}],
    },
    {
        "key": "rp-18",
        "case": "nothing stated",
        "message": "What is shown to me today?",
        "answer": answer(),
        "expect": [],
    },
    {
        "key": "rp-19",
        "case": "trap: a field off the allow-list",
        "message": "Please change my name to Ada Byron.",
        "answer": answer(
            item("preference", "display_name", {"text": "Ada Byron"}, "change my name to Ada Byron", "Name Ada Byron.")
        ),
        "expect": [],
    },
    {
        "key": "rp-20",
        "case": "trap: the same proposal twice",
        "message": "I also know Terraform well.",
        "answer": answer(
            item("fact", "skill", {"text": "Terraform"}, "I also know Terraform", "Knows Terraform."),
            item("fact", "skill", {"text": "terraform"}, "I also know Terraform", "Knows Terraform."),
        ),
        "expect": [{"entity": "fact", "operation": "create", "after": {"kind": "skill", "statement": "Terraform"}}],
    },
]


def prompt_version() -> str:
    from app.chat import proposals

    return proposals.PROMPT_VERSION


def write(root: Path = HERE) -> None:
    (root / "recordings").mkdir(exist_ok=True)
    version = prompt_version()
    manifest: dict[str, dict[str, str]] = {}
    gold = []
    for script in SCRIPTS:
        name = f"{script['key']}.json"
        (root / "recordings" / name).write_text(json.dumps([script["answer"]], indent=1) + "\n", encoding="utf-8")
        manifest[name] = {"record.propose": version}
        gold.append(
            {
                "key": script["key"],
                "labels": LABELS,
                "case": script["case"],
                "message": script["message"],
                "facts": script.get("facts", []),
                "declined": script.get("declined", []),
                "expect": script["expect"],
            }
        )
    (root / "gold.jsonl").write_text("".join(json.dumps(g) + "\n" for g in gold), encoding="utf-8")
    (root / "recordings" / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if str(HERE.parents[1] / "backend") not in sys.path:
        sys.path.insert(0, str(HERE.parents[1] / "backend"))
    write()
