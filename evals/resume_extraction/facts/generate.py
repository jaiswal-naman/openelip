"""Writes gold.jsonl: 120 invented resumes with the facts a careful reader finds in them.

    python evals/resume_extraction/facts/generate.py --seed 7

Everything is made up. The people, employers, schools and cities belong to nobody; the emails use the reserved domain
example.com and the phone numbers sit in ranges set aside for fiction. The same seed writes the same file, byte for
byte.

An item is {key, slice, synthetic, lines, protected_lines, expected: {facts}}. `lines` are what the resume reader would
return for a clean file, already in reading order: a single-column resume runs top to bottom, a two-column one gives the
left column then the right, a sidebar one gives the sidebar then the main column. Narrow columns wrap their text, so a
heading or a skill can span two lines. A fact is {kind, line_start, line_end, ...}: employer, title, institution,
degree or name as written, dates normalised (start_date, end_date) and as written (start_text, end_text), and `parent`,
the position in `facts` of the role an achievement belongs to. A skill also has `span`, the character range of the skill
in the lines joined by newlines.
"""

import argparse
import json
import random
import re
import textwrap
from pathlib import Path

HERE = Path(__file__).parent
COUNTS = {"single": 60, "two_column": 30, "sidebar": 30}
WIDTHS = {"single": (90, 90), "two_column": (62, 28), "sidebar": (70, 26)}  # (main column, narrow column)

FIRST = [
    "Mara",
    "Teodor",
    "Ines",
    "Callum",
    "Priya",
    "Joaquin",
    "Anneliese",
    "Kofi",
    "Saoirse",
    "Dmitri",
    "Yuki",
    "Layla",
    "Mateus",
    "Freya",
    "Omar",
    "Elodie",
    "Ravi",
    "Sunniva",
    "Tomasz",
    "Imani",
    "Bram",
    "Noor",
    "Lucia",
    "Anselm",
    "Thandi",
    "Kasper",
    "Odalys",
    "Ilya",
    "Marit",
    "Cormac",
]
LAST = [
    "Quillfeather",
    "Marrowby",
    "Hallowmere",
    "Pennyfold",
    "Corvane",
    "Dunmarrow",
    "Fennimore",
    "Gallowglass",
    "Harrowgate",
    "Ivermere",
    "Jessamine",
    "Kettleby",
    "Lorimar",
    "Mossgrave",
    "Nettlefold",
    "Oakhollow",
    "Pellmore",
    "Quenby",
    "Ravenscroft",
    "Strathmoor",
    "Thistlewick",
    "Underhay",
    "Valdemar",
    "Wexcombe",
    "Yarrowby",
    "Ashgrove",
    "Blythmoor",
    "Cadwallow",
    "Dovedale",
    "Elmsworth",
]
CITIES = ["Oakridge", "Millbrook", "Stonebridge", "Fairhaven", "Lakeshore", "Redfern", "Ashbourne", "Kingsmere"]
ROOTS = [
    "Veltrix",
    "Orbanta",
    "Nimbrel",
    "Tarvane",
    "Quillmont",
    "Brantley",
    "Zorvia",
    "Kestrel",
    "Lumenor",
    "Marrowfield",
    "Novalis",
    "Oriel",
    "Pelham",
    "Quorra",
    "Rhodane",
    "Sylvane",
    "Tessarune",
    "Umbria",
    "Valkyr",
    "Westerly",
    "Xanthe",
    "Yondra",
    "Zenthar",
    "Altrion",
    "Bexley",
    "Calder",
    "Dunmere",
    "Eskarra",
    "Fenwick",
    "Galbrith",
]
KINDS = [
    "Labs",
    "Systems",
    "Analytics",
    "Logistics",
    "Health",
    "Robotics",
    "Networks",
    "Capital",
    "Foods",
    "Media",
    "Energy",
    "Software",
    "Group",
    "Partners",
    "Studios",
    "Works",
]
EMPLOYERS = [f"{root} {kind}" for root in ROOTS for kind in KINDS]
TITLES = [
    "Software Engineer",
    "Senior Software Engineer",
    "Data Analyst",
    "Data Engineer",
    "Product Manager",
    "UX Designer",
    "DevOps Engineer",
    "Machine Learning Engineer",
    "Project Coordinator",
    "Account Manager",
    "Marketing Specialist",
    "Operations Manager",
    "Financial Analyst",
    "Customer Success Manager",
    "QA Engineer",
    "Technical Writer",
    "Systems Administrator",
    "Research Scientist",
    "Sales Representative",
    "HR Generalist",
    "Backend Developer",
    "Frontend Developer",
    "Security Analyst",
    "Business Analyst",
]
SCHOOLS = [
    "Eastvale Polytechnic",
    "Northfield Institute of Technology",
    "Ashbourne College",
    "Westmarch University",
    "Kingsmere Business School",
    "Harrowgate University",
    "Lakeshore Technical College",
    "Stonebridge University",
    "Redfern Institute",
    "Silverfield College",
]
DEGREES = [
    "B.Sc. Computer Science",
    "B.A. Economics",
    "M.Sc. Data Science",
    "B.Eng. Electrical Engineering",
    "MBA",
    "B.A. Graphic Design",
    "M.Sc. Statistics",
    "B.Sc. Mathematics",
    "Diploma in Business Administration",
    "B.A. Communications",
]
CERTS = [
    "Certified Scrum Master",
    "AWS Certified Cloud Practitioner",
    "Project Management Professional",
    "Google Data Analytics Certificate",
    "Certified Information Systems Auditor",
    "CompTIA Security+",
]
SKILLS = {
    "Languages": ["Python", "Go", "SQL", "TypeScript", "Java", "Rust", "Ruby", "Kotlin"],
    "Tools": [
        "Docker",
        "Terraform",
        "Airflow",
        "dbt",
        "Kubernetes",
        "Git",
        "Jenkins",
        "Grafana",
        "Excel",
        "Tableau",
        "Figma",
        "Jira",
    ],
    "Practice": [
        "Data modelling",
        "Stakeholder management",
        "Agile delivery",
        "Technical writing",
        "A/B testing",
        "Forecasting",
        "Accessibility review",
    ],
}
VERBS = [
    "Reduced",
    "Built",
    "Led",
    "Introduced",
    "Automated",
    "Migrated",
    "Designed",
    "Improved",
    "Launched",
    "Negotiated",
    "Mentored",
    "Coordinated",
]
OBJECTS = [
    "nightly batch run time",
    "the customer onboarding flow",
    "the reporting pipeline",
    "the incident rota",
    "the quarterly forecasting model",
    "vendor contracts",
    "the internal design system",
    "the support backlog",
    "checkout latency",
    "the release process",
]
TAILS = [
    "by 40%",
    "for 12 regional teams",
    "saving about 200 hours a year",
    "across three product lines",
    "with a team of five",
    "ahead of schedule",
    "",
]
AREAS = [
    "data platforms",
    "customer onboarding",
    "payments",
    "logistics software",
    "design systems",
    "revenue operations",
    "machine learning",
    "cloud infrastructure",
]
STATEMENTS = [
    "Known for clear documentation and steady delivery.",
    "Enjoys turning loose requirements into small, reliable services.",
    "Works best close to the people who use the product.",
]
PROJECTS = ["Orbit Dashboard", "Ledger Sync", "Harbor Planner", "Quill Notes", "Beacon Alerts", "Tidal Forecast"]
STYLES = ("iso", "mon", "long", "slash", "year")
MONTHS = "January February March April May June July August September October November December".split()


def squash(text):
    return " ".join(text.split())


def date_text(month_index, style):
    """month_index counts months from year 0 (year * 12 + month - 1)."""
    year, month = divmod(month_index, 12)
    month += 1
    return {
        "iso": f"{year}-{month:02d}",
        "mon": f"{MONTHS[month - 1][:3]} {year}",
        "long": f"{MONTHS[month - 1]} {year}",
        "slash": f"{month:02d}/{year}",
        "year": str(year),
    }[style]


def date_value(month_index, style):
    year, month = divmod(month_index, 12)
    return str(year) if style == "year" else f"{year}-{month + 1:02d}"


class Page:
    """The lines of one resume, built up in reading order."""

    def __init__(self):
        self.lines = []
        self.facts = []

    def add(self, text, width):
        """Append text wrapped to width. Returns the first and last line number it took."""
        wrapped = textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False) or [text]
        self.lines += wrapped
        return len(self.lines) - len(wrapped) + 1, len(self.lines)

    def fact(self, kind, lines, **fields):
        self.facts.append({"kind": kind, "line_start": lines[0], "line_end": lines[1], **fields})
        return len(self.facts) - 1


def person(rng):
    first, last = rng.choice(FIRST), rng.choice(LAST)
    phone = rng.choice([f"+1 (415) 555-01{rng.randint(0, 99):02d}", f"+44 7700 900{rng.randint(0, 999):03d}"])
    return {
        "name": f"{first} {last}",
        "email": f"{first}.{last}@example.com".lower(),
        "phone": phone,
        "city": rng.choice(CITIES),
    }


def jobs(rng):
    """Two to four roles, newest first, with a gap of a few months between them so no two overlap."""
    month_index = 2025 * 12 + rng.randint(0, 11)
    found = []
    for position in range(rng.randint(2, 4)):
        start = month_index - rng.randint(14, 54)
        current = position == 0 and rng.random() < 0.6
        found.append({"start": start, "end": None if current else month_index})
        month_index = start - rng.randint(1, 4)
    return found


def experience(page, rng, width, style):
    page.add("EXPERIENCE", width)
    separator = rng.choice([" - ", " – ", " to "])
    heading = rng.choice(["comma", "pipe", "at"])
    for job, employer, title in zip(jobs(rng), rng.sample(EMPLOYERS, 4), rng.sample(TITLES, 4)):
        start_text = date_text(job["start"], style)
        end_text = rng.choice(["Present", "Present", "Current"]) if job["end"] is None else date_text(job["end"], style)
        dates = f"{start_text}{separator}{end_text}"
        if heading == "comma":
            first, last = page.add(f"{title}, {employer}   {dates}", width)
        elif heading == "pipe":
            first = page.add(f"{employer} | {title}", width)[0]
            last = page.add(dates, width)[1]
        else:
            first, last = page.add(f"{title} at {employer} ({dates})", width)
        role = page.fact(
            "role",
            (first, last),
            parent=None,
            employer=employer,
            title=title,
            start_date=date_value(job["start"], style),
            end_date=None if job["end"] is None else date_value(job["end"], style),
            start_text=start_text,
            end_text=end_text,
        )
        for _ in range(rng.randint(1, 4)):
            bullet = f"- {rng.choice(VERBS)} {rng.choice(OBJECTS)} {rng.choice(TAILS)}".strip()
            page.fact("achievement", page.add(bullet, width), parent=role)


def education(page, rng, width, stacked):
    page.add("EDUCATION", width)
    for degree, school in zip(rng.sample(DEGREES, 2)[: rng.randint(1, 2)], rng.sample(SCHOOLS, 2)):
        end = rng.randint(2005, 2023)
        both = rng.random() < 0.7
        start_text, end_text = (str(end - rng.randint(2, 4)) if both else None), str(end)
        dates = f"{start_text} - {end_text}" if both else end_text
        if stacked:
            first = page.add(degree, width)[0]
            page.add(school, width)
            last = page.add(dates, width)[1]
        else:
            first, last = page.add(f"{degree}, {school}, {dates}", width)
        page.fact(
            "education",
            (first, last),
            institution=school,
            degree=degree,
            start_date=start_text,
            end_date=end_text,
            start_text=start_text,
            end_text=end_text,
        )


def skills(page, rng, width):
    page.add("SKILLS", width)
    for label, pool in SKILLS.items():
        chosen = rng.sample(pool, rng.randint(2, 5))
        first, last = page.add(f"{label}: " + ", ".join(chosen), width)
        block = "\n".join(page.lines[first - 1 : last])
        offset = sum(len(line) + 1 for line in page.lines[: first - 1])
        cursor = 0
        for name in chosen:  # find each skill where it is written, whole words only, left to right
            found = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, name.split())) + r"(?!\w)").search(block, cursor)
            cursor = found.end()
            lines = [first + block.count("\n", 0, found.start()), first + block.count("\n", 0, found.end() - 1)]
            page.fact("skill", lines, name=name, span=[offset + found.start(), offset + found.end()])


def certifications(page, rng, width, stacked):
    page.add("CERTIFICATIONS", width)
    for name in rng.sample(CERTS, rng.randint(1, 2)):
        year = str(rng.randint(2015, 2024))
        if stacked:
            first = page.add(name, width)[0]
            last = page.add(year, width)[1]
        else:
            first, last = page.add(f"{name}, {year}", width)
        page.fact("certification", (first, last), name=name, end_date=year, end_text=year)


def project(page, rng, width):
    page.add("PROJECTS", width)
    name, year = rng.choice(PROJECTS), str(rng.randint(2019, 2024))
    first = page.add(f"Project: {name} ({year})", width)[0]
    last = page.add(f"{rng.choice(VERBS)} {rng.choice(OBJECTS)} as a side project.", width)[1]
    page.fact("project", (first, last), parent=None, title=name, end_date=year, end_text=year)


def personal(page, rng, width):
    """In about one resume in ten, a line that states a protected characteristic. It is no fact, so a reader that
    cites it is wrong and the check in code must decline it (REC-07). Returns its line number, if there is one."""
    if rng.random() >= 0.1:
        return []
    return [page.add(rng.choice(["Marital status: married", "Date of birth: 14/02/1991"]), width)[0]]


def summary(page, rng, width):
    area = rng.choice(AREAS)
    text = f"{rng.choice(TITLES)} with {rng.randint(3, 15)} years of experience in {area}. {rng.choice(STATEMENTS)}"
    page.fact("other", page.add(text, width), label="summary")


def resume(rng, key, layout):
    main, narrow = WIDTHS[layout]
    style, who, page = rng.choice(STYLES), person(rng), Page()
    wants = {name: rng.random() < odds for name, odds in (("summary", 0.7), ("certs", 0.35), ("project", 0.3))}
    stacked = layout != "single"  # a narrow column puts a degree, a school and its dates on lines of their own
    protected = []
    if layout == "sidebar":  # the sidebar is read first, then the main column
        for line in (f"Email: {who['email']}", f"Phone: {who['phone']}", f"Location: {who['city']}"):
            page.add(line, narrow)
        skills(page, rng, narrow)
        education(page, rng, narrow, stacked)
        if wants["certs"]:
            certifications(page, rng, narrow, stacked)
        protected = personal(page, rng, narrow)
        page.add(who["name"].upper(), main)
        if wants["summary"]:
            summary(page, rng, main)
        experience(page, rng, main, style)
        if wants["project"]:
            project(page, rng, main)
    else:
        page.add(who["name"], main)
        page.add(f"{who['email']} | {who['phone']} | {who['city']}", main)
        if wants["summary"]:
            summary(page, rng, main)
        experience(page, rng, main, style)
        if wants["project"]:
            project(page, rng, main)
        education(page, rng, narrow, stacked)
        skills(page, rng, narrow)
        if wants["certs"]:
            certifications(page, rng, narrow, stacked)
        protected = personal(page, rng, narrow)
    return {
        "key": key,
        "slice": layout,
        "synthetic": True,
        "lines": page.lines,
        "protected_lines": protected,
        "expected": {"facts": page.facts},
    }


def check(item):
    """Every name, date and skill a fact claims is written in the lines it cites (a mistake here would be silent)."""
    lines, text = item["lines"], "\n".join(item["lines"])
    for fact in item["expected"]["facts"]:
        assert 1 <= fact["line_start"] <= fact["line_end"] <= len(lines), (item["key"], fact)
        cited = squash("\n".join(lines[fact["line_start"] - 1 : fact["line_end"]])).lower()
        for field in ("employer", "title", "institution", "degree", "name", "start_text", "end_text"):
            if fact.get(field):
                assert squash(fact[field]).lower() in cited, (item["key"], fact["kind"], field)
        if "span" in fact:
            assert squash(text[fact["span"][0] : fact["span"][1]]) == squash(fact["name"]), (item["key"], fact)


def build(seed):
    rng = random.Random(seed)
    layouts = [name for name, count in COUNTS.items() for _ in range(count)]
    rng.shuffle(layouts)
    items = [resume(rng, f"resume-{n:03d}", layout) for n, layout in enumerate(layouts, 1)]
    for item in items:
        check(item)
    return items


def main():
    parser = argparse.ArgumentParser(description="Write the invented gold set.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=HERE / "gold.jsonl")
    args = parser.parse_args()
    items = build(args.seed)
    args.out.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8", newline="\n")
    print(f"wrote {len(items)} invented resumes to {args.out.name}")


if __name__ == "__main__":
    main()
