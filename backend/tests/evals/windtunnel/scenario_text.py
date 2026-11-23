# ruff: noqa: E501  (TOML lines of the sample scenario)
"""A minimal valid scenario file for the loader, world and driver tests, written into a temporary folder."""

from pathlib import Path

MATCH = """
key = "{key}"
version = 1
persona = "cooperative"
title = "A test scenario (invented)"
start = "{start}"
labels = "agent-drafted, maintainer review pending"
invented = true
max_virtual_days = 20

[world.job]
title = "Data engineer"
company = "Lumen Freight"
description = "Build reporting pipelines in Python. Experience with PostgreSQL."
apply_url = "https://careers.{host}/apply/1"
requirements = [
  {{ text = "Build reporting pipelines in Python", importance = "required" }},
  {{ text = "Experience with PostgreSQL", importance = "required" }},
]
country = "US"
arrangements = ["remote"]

[world.candidate]
name = "Ada Example"
follow_up_after_days = 7
resume = [
  "Ada Example",
  "Senior data engineer, Initech Demo, 2021-03 to 2024-06",
  "Built nightly Python and PostgreSQL pipelines that cut report time by 40 percent.",
  "Call me on +1 415 555 0100.",
]
facts = [
  {{ kind = "role", statement = "Senior data engineer, Initech Demo, 2021-03 to 2024-06", attributes = {{ employer = "Initech Demo", title = "Senior data engineer", start_date = "2021-03", end_date = "2024-06" }} }},
  {{ kind = "achievement", statement = "Built nightly Python and PostgreSQL pipelines that cut report time by 40 percent." }},
]
constraints = [
  {{ kind = "location", statement = "Only jobs in the United States.", country_codes = ["US"] }},
  {{ kind = "salary_floor", statement = "At least 2400000 a year.", salary_floor = 2400000, salary_currency = "USD" }},
]

[world.canaries]
phone = "+1 415 555 0100"
current_pay = "1850000"
salary_floor = "2400000"

[world.employer]
contact = "talent@{host}"
organization = "Lumen Demo"
answer_reason = "A good fit for the team."

[world.employer.role]
title = "Data engineer"
description = "Build reporting pipelines in Python. Experience with PostgreSQL."
requirements = [
  {{ text = "Build reporting pipelines in Python", importance = "required" }},
  {{ text = "Experience with PostgreSQL", importance = "required" }},
]

[inject]

[[candidate]]
on = "start"
do = "start_campaign"

[[employer]]
on = "mail:follow_up_v1:1"
do = "reply interview"

[expect]
goal = "stage:interview"
"""


def write(
    directory: Path, key: str = "unit-a", *, start: str = "match", host: str = "lumen.example", extra: str = ""
) -> Path:
    """<directory>/<key>.toml and its recorded/ folder; `extra` is appended to the file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / key / "recorded").mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.toml"
    path.write_text(MATCH.format(key=key, start=start, host=host) + extra, encoding="utf-8")
    return path
