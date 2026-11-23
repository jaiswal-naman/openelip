"""Invented candidates for the tailoring tests. Nobody here exists: the people, employers and tools are made up, and the
facts are shaped like the rows profile.api.usable_facts returns (kind, statement, attributes, parent, source).

The ids are readable words, not UUIDs, so a test can search a message for them.
"""

from typing import Any

from app.tailor.resume import Fact

RESUME, EDIT, CHAT = "resume", "candidate_edit", "confirmed_proposal"


def fact(
    fact_id: str,
    kind: str,
    statement: str,
    *,
    parent: str | None = None,
    source: str = RESUME,
    **attributes: Any,
) -> Fact:
    return Fact(fact_id, kind, statement, attributes, parent, source)


def role(fact_id: str, employer: str, title: str, start: str, end: str | None = None, **more: Any) -> Fact:
    """A role as Phase 3 stores it: the statement is the resume's own heading lines, the attributes are checked values."""
    attributes: dict[str, Any] = {"employer": employer, "title": title, "start_date": start, **more}
    if end:
        attributes["end_date"] = end
    dates = f"{start} - {end}" if end else f"{start} - Present"
    return fact(fact_id, "role", f"{employer} | {title}\n{dates}", **attributes)


def bullet(fact_id: str, statement: str, parent: str, **more: Any) -> Fact:
    return fact(fact_id, "achievement", f"- {statement}", parent=parent, **more)


def skill(fact_id: str, name: str, *, source: str = RESUME) -> Fact:
    return fact(fact_id, "skill", name, source=source, name=name)


def candidate() -> list[Fact]:
    """Two roles with bullets, three skills, a degree, a certification and one skill stated in chat (not on the
    resume). The order is the order profile.api.usable_facts gives: document order."""
    return [
        role("role-initech", "Initech Demo", "Backend Engineer", "2019-03", "2022-08"),
        bullet("b-python", "Built Python services for payments", "role-initech"),
        bullet("b-report", "Cut report build time by 30%", "role-initech"),
        bullet("b-checkout", "Contributed to the checkout migration", "role-initech"),
        bullet("b-mentor", "Mentored one junior developer", "role-initech"),
        role("role-globex", "Globex Demo", "Data Analyst", "2016-01", "2019-02"),
        bullet("b-sql", "Wrote weekly SQL reports for two teams", "role-globex"),
        bullet("b-invoice", "Automated the invoice export", "role-globex"),
        skill("s-python", "Python"),
        skill("s-sql", "SQL"),
        skill("s-go", "Go"),
        fact(
            "edu-lakeshore",
            "education",
            "B.Sc. Computer Science\nLakeshore Technical College\n2012 - 2016",
            degree="B.Sc. Computer Science",
            institution="Lakeshore Technical College",
            start_date="2012",
            end_date="2016",
        ),
        fact("cert-scrum", "certification", "Certified Scrum Master\n2018", name="Certified Scrum Master", end_date="2018"),
        skill("s-quill", "Quillscript", source=CHAT),
    ]


def requirements() -> list[Any]:
    """What an invented posting asks for. Kubernetes and Terraform are in no fact of the candidate."""
    from app.tailor.verifier import Requirement  # Task 2's module: imported when asked for, so Task 1's tests need not

    return [
        Requirement(101, "Experience building Python services", "required"),
        Requirement(102, "Kubernetes in production", "required"),
        Requirement(103, "Terraform or similar infrastructure tooling", "preferred"),
        Requirement(104, "Clear written communication", "preferred"),
    ]
