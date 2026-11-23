"""The Analyst (plan 07-02, CMP-02 side): requirements grouped by stored verdicts, no model call."""

from types import SimpleNamespace

from app.campaigns.crew import fit
from app.tailor import api as tailor


def row(requirement_id, verdict, *facts):
    return SimpleNamespace(requirement_id=requirement_id, verdict=verdict, fact_ids=facts)


REQUIREMENTS = [
    tailor.Requirement(1, "Build pipelines in Python", "required"),
    tailor.Requirement(2, "Experience with Kubernetes", "required"),
    tailor.Requirement(3, "Communicate with stakeholders", "preferred"),
    tailor.Requirement(4, "Know SQL", "required"),
    tailor.Requirement(5, "Mentor juniors", "preferred"),
    tailor.Requirement(6, "Write documentation", "required"),
    tailor.Requirement(7, "Use dbt", "required"),
    tailor.Requirement(8, "Know Airflow", "required"),
]


def test_requirements_are_grouped_by_verdict_with_fact_ids_and_unscored_ones_are_not_assessed():
    rows = [
        row(1, "met", "f-a", "f-b"),
        row(2, "no_evidence"),
        row(3, "partly_met", "f-c"),
        row(4, "met", "f-d"),
        row(5, "met", "f-e"),
        row(6, "not_met"),
        row(7, "bogus verdict", "f-x"),  # not a verdict this code knows: not assessed, never guessed
    ]
    result = fit.analyse(rows, REQUIREMENTS)
    assert result["met"] == [
        {"requirement_id": 1, "fact_ids": ["f-a", "f-b"]},
        {"requirement_id": 4, "fact_ids": ["f-d"]},
        {"requirement_id": 5, "fact_ids": ["f-e"]},
    ]
    assert result["partly_met"] == [{"requirement_id": 3, "fact_ids": ["f-c"]}]
    assert result["not_met"] == [{"requirement_id": 6, "fact_ids": []}]
    assert result["no_evidence"] == [{"requirement_id": 2, "fact_ids": []}]
    assert [n["requirement_id"] for n in result["not_assessed"]] == [7, 8]


def test_talking_points_are_three_met_requirements_required_ones_first():
    rows = [row(5, "met", "f-e"), row(1, "met", "f-a"), row(4, "met", "f-d"), row(6, "met", "f-f")]
    result = fit.analyse(rows, REQUIREMENTS)
    assert [p["requirement_id"] for p in result["talking_points"]] == [1, 4, 6]  # 5 is preferred: it waits
    assert fit.analyse([], REQUIREMENTS)["talking_points"] == []
    assert len(fit.analyse([], REQUIREMENTS)["not_assessed"]) == 8
