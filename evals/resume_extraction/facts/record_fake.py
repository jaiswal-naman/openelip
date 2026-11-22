"""Builds recordings.jsonl: the answers for every gold resume, passed through a fixed fault injector.

    cd backend && uv run python ../evals/resume_extraction/facts/record_fake.py

No model is called. The answers are the gold truth written as the three tasks' JSON, and about 8% of the items are
damaged on purpose: a line range that points at the wrong lines, an employer, school or skill that the cited lines do
not contain, or dates swapped so they run backwards. The checks in code must catch what they can, and the score says how
much they did. A resume with a protected-characteristic line gets one more item citing that line, the kind of item a
careless reader returns and the check in code must drop (REC-07).

Each recording is keyed by the hash of the request it answers (backend/app/profile/extract/fake.py), so changing a
prompt, the contact masking or the gold set means running this script again. It is not a measurement of any model.
"""

import argparse
import json
import random
from pathlib import Path

from app.profile.extract import fake, workflow

HERE = Path(__file__).parent
FAULT_RATE = 0.08
SEED = 11
INVENTED = ["Zentrova Holdings", "Quantelle Dynamics", "Mirador Freight", "Obsidian Pharma", "Tessera Aerospace"]
ROLES, EDUCATION_SKILLS, SUMMARY = (task.name for task in workflow.TASKS)


def role_item(fact, parent):
    return {
        "kind": fact["kind"],
        "line_start": fact["line_start"],
        "line_end": fact["line_end"],
        "parent": parent,
        "employer": fact.get("employer"),
        "title": fact.get("title"),
        "start_date": fact.get("start_text"),
        "end_date": fact.get("end_text"),
        "is_current": True if fact.get("end_text") in ("Present", "Current") else None,
    }


def study_item(fact):
    return {
        "kind": fact["kind"],
        "line_start": fact["line_start"],
        "line_end": fact["line_end"],
        "institution": fact.get("institution"),
        "degree": fact.get("degree"),
        "name": fact.get("name"),
        "start_date": fact.get("start_text"),
        "end_date": fact.get("end_text"),
    }


def damage(item, rng):
    """One fault, chosen among those that fit the item. A wrong range may also fall off the end of the resume."""
    options = ["range"]
    named = [field for field in ("employer", "institution", "name") if item.get(field)]
    if named:
        options.append("name")
    if item.get("start_date") and item.get("end_date") and not item.get("is_current"):
        options.append("dates")
    fault = rng.choice(options)
    if fault == "range":
        shift = rng.choice((2, 3, 4))
        item["line_start"] += shift
        item["line_end"] += shift
    elif fault == "name":
        item[named[0]] = rng.choice(INVENTED)
    else:
        item["start_date"], item["end_date"] = item["end_date"], item["start_date"]


def answers(gold_item, rng, rate):
    """The answer to each task for one resume, as the tasks' JSON. Returns {task name: answer}."""
    facts = gold_item["expected"]["facts"]
    in_roles = [n for n, fact in enumerate(facts) if fact["kind"] in ("role", "achievement", "project")]
    position = {n: place for place, n in enumerate(in_roles)}  # a truth fact's place among the roles call's items
    roles = [
        role_item(facts[n], None if facts[n]["parent"] is None else position[facts[n]["parent"]]) for n in in_roles
    ]
    studies = [study_item(fact) for fact in facts if fact["kind"] in ("education", "skill", "certification")]
    statements = [{"line_start": f["line_start"], "line_end": f["line_end"]} for f in facts if f["kind"] == "other"]
    statements += [{"line_start": n, "line_end": n} for n in gold_item["protected_lines"]]  # the careless extra item
    for item in roles + studies + statements:
        if rng.random() < rate:
            damage(item, rng)
    return {ROLES: {"items": roles}, EDUCATION_SKILLS: {"items": studies}, SUMMARY: {"items": statements}}


def record(gold, rate=FAULT_RATE, seed=SEED):
    """One entry per resume and task: {key, task, request, answers}. Always the same entries for the same arguments."""
    rng = random.Random(seed)
    entries = []
    for gold_item in gold:
        given = answers(gold_item, rng, rate)
        for task, messages in workflow.build_requests(fake.result_from_lines(gold_item["lines"])):
            entries.append(
                {
                    "key": gold_item["key"],
                    "task": task.name,
                    "request": fake.request_hash(task.name, messages),
                    "answers": [given[task.name]],
                }
            )
    return entries


def write(entries, path):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description="Write the recorded answers for the invented gold set.")
    parser.add_argument("--fault-rate", type=float, default=FAULT_RATE)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=Path, default=HERE / "recordings.jsonl")
    args = parser.parse_args()
    gold = [json.loads(line) for line in (HERE / "gold.jsonl").read_text(encoding="utf-8").splitlines()]
    entries = record(gold, args.fault_rate, args.seed)
    write(entries, args.out)
    print(f"wrote {len(entries)} recordings for {len(gold)} resumes to {args.out.name}")


if __name__ == "__main__":
    main()
