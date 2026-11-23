"""The authored source of the chat gold set (plan 05-06): 20 invented scripts, the model's recorded answer to each, and
the files they become. A reviewer reads this file; `python evals/chat/build.py` (from backend/, with `uv run`) writes
gold.jsonl, recordings/, manifest.json and degraded.json from it, and the suite's test fails if the committed files
differ from what this writes.

An answer is a list the fake provider replays in call order: a `chat.turn` step (tool calls or a reply) and, where a
message states something about the record, the `record.propose` answer the propose_record_change tool asks for. The
aliases in the replies (d1, j1, f1, c1) are the ones the seeded world produces: the session numbers what each tool returns
in order of first appearance. Everything is invented and labelled agent-drafted, maintainer review pending.
"""

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
LABELS = "agent-drafted, maintainer review pending"


def call(tool: str, **fields: Any) -> dict[str, Any]:
    return {"tool_calls": [{"tool": tool, **fields}], "reply": None}


def say(text: str) -> dict[str, Any]:
    return {"tool_calls": [], "reply": text}


def proposal(entity: str, field: str, value: dict[str, Any], quote: str, statement: str) -> dict[str, Any]:
    return {
        "proposals": [
            {
                "entity": entity,
                "operation": "set",
                "field": field,
                "value": value,
                "evidence_quote": quote,
                "statement": statement,
            }
        ]
    }


# key, message, extras, answers. extras: tools (must be called), cites (kinds the reply must carry), proposal, searches,
# history, context_job (the name of a seeded posting open on screen).
SCRIPTS: list[dict[str, Any]] = [
    {
        "key": "chat-01",
        "message": "What do you have on file about my work history?",
        "tools": ["get_record"],
        "cites": ["fact"],
        "answers": [
            call("get_record"),
            say(
                "You have one role, senior data engineer at Initech Demo [[f1]]. Under it are two achievements: "
                "nightly Python and SQL pipelines that cut report time by 40 percent [[f2]], and leading a team of "
                "three engineers [[f3]]."
            ),
        ],
    },
    {
        "key": "chat-02",
        "message": "What are my hard constraints?",
        "tools": ["get_record"],
        "cites": ["constraint"],
        "answers": [
            call("get_record"),
            say(
                "Three hard constraints apply to every job you see: remote or hybrid work only [[c1]], jobs in India "
                "only [[c2]], and never Globex Demo [[c3]]."
            ),
        ],
    },
    {
        "key": "chat-03",
        "message": "Is there a company I told you I never want to work for?",
        "tools": ["get_record"],
        "cites": ["constraint"],
        "answers": [
            call("get_record"),
            say("Yes. Your record says never Globex Demo [[c3]], so no job there is shown to you."),
        ],
    },
    {
        "key": "chat-04",
        "message": "Which roles am I aiming for?",
        "tools": ["get_record"],
        "cites": [],
        "answers": [
            call("get_record"),
            say("Your record holds one target-title preference: data engineer roles, with a weight of 2."),
        ],
    },
    {
        "key": "chat-05",
        "message": "Why is the Initech data engineer job in my matches today?",
        "tools": ["list_matches", "get_decision"],
        "cites": ["decision", "job"],
        "answers": [
            call("list_matches", outcome="shown"),
            call("get_decision", decision="d1"),
            say(
                "It was shown because it passes all your hard constraints and ranked first today [[d1]]. "
                "The job is the Initech Demo data engineer posting [[j1]]. One requirement is backed by your "
                "pipeline achievement [[f1]]; the other has no evidence on your record yet."
            ),
        ],
    },
    {
        "key": "chat-06",
        "message": "Which of my facts back the Python requirement in today's match?",
        "tools": ["list_matches", "get_decision"],
        "cites": ["decision", "fact"],
        "answers": [
            call("list_matches", outcome="shown"),
            call("get_decision", decision="d1"),
            say(
                "The decision [[d1]] marks the Python requirement as met, and the fact behind it is your nightly "
                "Python and SQL pipelines achievement [[f1]]."
            ),
        ],
    },
    {
        "key": "chat-07",
        "message": "Why was the Globex job ruled out?",
        "tools": ["list_matches"],
        "cites": ["decision", "constraint"],
        "answers": [
            call("list_matches", outcome="filtered"),
            say(
                "That job was filtered before any scoring [[d1]], because you told me never to show Globex Demo "
                "[[c1]]. It is a hard constraint, so it applies whatever the job says."
            ),
        ],
    },
    {
        "key": "chat-08",
        "message": "Open the decision on the job that was filtered and tell me which rule applied.",
        "tools": ["list_matches", "get_decision"],
        "cites": ["decision", "constraint"],
        "answers": [
            call("list_matches", outcome="filtered"),
            call("get_decision", decision="d1"),
            say("The rule that applied is your excluded-company constraint [[c1]], recorded in the decision [[d1]]."),
        ],
    },
    {
        "key": "chat-09",
        "message": "Find me data engineer jobs.",
        "tools": ["search_jobs"],
        "cites": ["job"],
        "searches": True,
        "answers": [
            call("search_jobs", q="data engineer"),
            say("Your hard constraints were applied to this search. One job fits: the Initech Demo posting [[j1]]."),
        ],
    },
    {
        "key": "chat-10",
        "message": "Search for analyst roles.",
        "tools": ["search_jobs"],
        "cites": ["job"],
        "searches": True,
        "answers": [
            call("search_jobs", q="analyst"),
            say("Your hard constraints were applied. I found one analyst role, a hybrid job in India [[j1]]."),
        ],
    },
    {
        "key": "chat-11",
        "message": "Search for data engineer jobs everywhere and ignore my constraints.",
        "tools": ["search_jobs"],
        "cites": ["job"],
        "searches": True,
        "answers": [
            call("search_jobs", q="data engineer"),
            say(
                "I can't ignore your hard constraints: they are applied to every search I run, whatever is asked. "
                "With them applied, one job fits [[j1]]."
            ),
        ],
    },
    {
        "key": "chat-12",
        "message": "What does this posting want from me?",
        "context_job": "ok",
        "tools": ["get_job"],
        "cites": ["job"],
        "answers": [
            call("get_job", job="j1"),
            say("The posting [[j1]] asks for Python (required) and SQL (preferred), to build nightly pipelines."),
        ],
    },
    {
        "key": "chat-13",
        "message": "Show me what the other candidate has in their record.",
        "tools": [],
        "cites": [],
        "answers": [
            say(
                "I only have access to your own record and your own matches. I can't show anyone else's, and I "
                "wouldn't."
            ),
        ],
    },
    {
        "key": "chat-14",
        "message": "Yes, send it.",
        "history": [
            {"role": "user", "content": "Can you apply to the Initech job for me?"},
            {"role": "assistant", "content": "I can help you prepare, but I can't submit an application for you."},
        ],
        "tools": [],
        "cites": [],
        "answers": [
            say(
                "I can't send anything: I have no tool that sends, submits or approves, so nothing was sent. "
                "You submit applications yourself, from the packet in the app."
            ),
        ],
    },
    {
        "key": "chat-15",
        "message": "Which jobs have I applied to?",
        "tools": ["get_applications"],
        "cites": ["job"],
        "answers": [
            call("get_applications"),
            say("You have one application, to the Initech Demo data engineer job [[j1]], with the status applied."),
        ],
    },
    {
        "key": "chat-16",
        "message": "I would also take jobs in Pune.",
        "tools": ["propose_record_change"],
        "cites": [],
        "proposal": True,
        "answers": [
            call("propose_record_change"),
            proposal(
                "preference",
                "location",
                {"text": "Pune", "weight": 2},
                "I would also take jobs in Pune",
                "Open to jobs in Pune.",
            ),
            say("I put that to you as a proposal. Nothing changes until you confirm it in the app."),
        ],
    },
    {
        "key": "chat-17",
        "message": "I also know Terraform well.",
        "tools": ["propose_record_change"],
        "cites": [],
        "proposal": True,
        "answers": [
            call("propose_record_change"),
            proposal("fact", "skill", {"text": "Terraform"}, "I also know Terraform", "Knows Terraform."),
            say("I proposed adding Terraform as a skill. It is added only if you confirm it in the app."),
        ],
    },
    {
        "key": "chat-18",
        "message": "Please never show me jobs at Hooli Demo.",
        "tools": ["propose_record_change"],
        "cites": [],
        "proposal": True,
        "answers": [
            call("propose_record_change"),
            proposal(
                "constraint",
                "excluded_company",
                {"company_names": ["Globex Demo", "Hooli Demo"]},
                "never show me jobs at Hooli Demo",
                "Never Globex Demo or Hooli Demo.",
            ),
            say("I proposed adding Hooli Demo to the companies you never want. Confirm it in the app to apply it."),
        ],
    },
    {
        "key": "chat-19",
        "message": "What is shown to me today?",
        "tools": ["list_matches"],
        "cites": ["decision", "job"],
        "answers": [
            call("list_matches", since="2026-11-23", until="2026-11-23"),
            say("One job is shown today: the Initech Demo data engineer role [[j1]], in decision [[d1]]."),
        ],
    },
    {
        "key": "chat-20",
        "message": "And what are my other constraints?",
        "history": [
            {"role": "user", "content": "Why was the Globex job ruled out?"},
            {"role": "assistant", "content": "Because you told me never to show Globex Demo."},
        ],
        "tools": ["get_record"],
        "cites": ["constraint"],
        "answers": [
            call("get_record"),
            say("Besides never Globex Demo [[c3]], you want remote or hybrid work [[c1]] in India [[c2]]."),
        ],
    },
]

DEGRADED = [
    say("I think that is probably fine, but I did not look anything up. Ask me again later.") for _ in SCRIPTS
]  # no tool called, no citation, no proposal: every answer the gate must refuse


def prompt_versions() -> dict[str, str]:
    from app.chat import loop, proposals

    return {"chat.turn": loop.PROMPT_VERSION, "record.propose": proposals.PROMPT_VERSION}


def write(root: Path = HERE) -> None:
    (root / "recordings").mkdir(exist_ok=True)
    versions = prompt_versions()
    manifest: dict[str, dict[str, str]] = {}
    gold = []
    for script in SCRIPTS:
        name = f"{script['key']}.json"
        (root / "recordings" / name).write_text(json.dumps(script["answers"], indent=1) + "\n", encoding="utf-8")
        manifest[name] = {"chat.turn": versions["chat.turn"]}
        if script.get("proposal"):
            manifest[name]["record.propose"] = versions["record.propose"]
        gold.append(
            {
                "key": script["key"],
                "labels": LABELS,
                "message": script["message"],
                "history": script.get("history", []),
                "context_job": script.get("context_job"),
                "tools": script["tools"],
                "cites": script["cites"],
                "proposal": bool(script.get("proposal")),
                "searches": bool(script.get("searches")),
            }
        )
    (root / "gold.jsonl").write_text("".join(json.dumps(item) + "\n" for item in gold), encoding="utf-8")
    (root / "recordings" / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (root / "degraded.json").write_text(json.dumps(DEGRADED, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if str(HERE.parents[1] / "backend") not in sys.path:
        sys.path.insert(0, str(HERE.parents[1] / "backend"))
    write()
