"""The authored source of the tailoring-integrity gold set: six invented candidates, thirty invented postings, and for
every pair the edits a model proposed with the label of each. build_gold.py turns this into gold.jsonl, the recordings
and the manifest, so a reviewer reads (and a second rater labels) this file and GUIDELINE.md, not generated JSON.

Everything here is invented: the people, employers, tools and postings do not exist. Labels are agent-drafted,
maintainer review pending. An edit that carries a `reason` is labelled unsupported and says which temptation it is
(`kind`); an edit with no reason is supported. A fact is named by its tag (`b1`, `py`); build_gold.py numbers the
facts f1..fN in the order a candidate lists them. `reqs` are 1-based positions in the pair's requirement list.
"""

from typing import Any

LABELS = "agent-drafted, maintainer review pending"
MONTHS = (
    "January February March April May June July August September October November December".split()
)
VERDICTS = {"m": "met", "p": "partly_met", "n": "no_evidence", "x": "not_met"}


def _written(date: str) -> str:
    """'2019-03' as 'March 2019', '2019' as '2019'."""
    year, _, month = date.partition("-")
    return f"{MONTHS[int(month) - 1]} {year}" if month else year


def fact(tag: str, kind: str, statement: str, attributes: dict[str, Any], parent: str | None, chat: bool) -> dict:
    source = "confirmed_proposal" if chat else "resume"
    return {"tag": tag, "kind": kind, "statement": statement, "attributes": attributes, "parent": parent, "source": source}


def role(tag: str, employer: str, title: str, start: str, end: str | None = None) -> dict:
    attributes: dict[str, Any] = {"employer": employer, "title": title, "start_date": start}
    attributes.update({"end_date": end} if end else {"is_current": True})
    dates = f"{_written(start)} - {_written(end) if end else 'Present'}"
    return fact(tag, "role", f"{employer} | {title}\n{dates}", attributes, None, False)


def bullet(tag: str, parent: str, text: str) -> dict:
    return fact(tag, "achievement", f"- {text}", {}, parent, False)


def skill(tag: str, name: str, *, chat: bool = False) -> dict:
    return fact(tag, "skill", name, {"name": name}, None, chat)


def degree(tag: str, name: str, institution: str, start: str, end: str) -> dict:
    attributes = {"degree": name, "institution": institution, "start_date": start, "end_date": end}
    return fact(tag, "education", f"{name}\n{institution}\n{start} - {end}", attributes, None, False)


def cert(tag: str, name: str, year: str) -> dict:
    return fact(tag, "certification", f"{name}\n{year}", {"name": name, "end_date": year}, None, False)


def _edit(op: str, target: str | None, after: str | None, support: list[str], order: list[str], **label: Any) -> dict:
    reason = label.get("reason")
    kind = label.get("kind") or (f"clean_{op.split('_')[0]}" if reason is None else "")
    assert reason is None or label.get("kind"), "an unsupported edit says which temptation it is"
    return {
        "op": op, "target": target, "after": after, "support": support, "order": order,
        "label": "supported" if reason is None else "unsupported", "kind": kind, "reason": reason,
        "why": label.get("why", ""), "reqs": list(label.get("reqs", ())),
    }  # fmt: skip


def rephrase(target: str, after: str, support: list[str] | None = None, **label: Any) -> dict:
    return _edit("rephrase", target, after, [target] if support is None else support, [], **label)


def reorder(order: list[str], **label: Any) -> dict:
    return _edit("reorder", None, "", [], order, **label)


def remove(target: str, **label: Any) -> dict:
    return _edit("remove", target, "", [target], [], **label)


def add(tag: str, text: str | None = None, **label: Any) -> dict:
    """add_from_fact; the text is the fact's own words unless one is given (a paraphrase, which is the temptation)."""
    return _edit("add_from_fact", None, text, [tag], [], **label)


def pair(number: int, candidate: str, requirements: list[tuple], edits: list[dict], *, rephrase: bool = True) -> dict:
    return {
        "key": f"ti-{number:03d}",
        "candidate": candidate,
        "allow_rephrase": rephrase,
        "requirements": [
            {"importance": {"req": "required", "pref": "preferred"}[importance], "text": text, "verdict": VERDICTS.get(code)}
            for importance, text, code in requirements
        ],
        "edits": edits,
    }


CANDIDATES: dict[str, list[dict]] = {
    # a backend engineer
    "bk": [
        role("r1", "Brindle Demo", "Backend Engineer", "2019-03", "2022-08"),
        bullet("b1", "r1", "Built Python services for payments"),
        bullet("b2", "r1", "Cut report build time by 30%"),
        bullet("b3", "r1", "Contributed to the checkout migration"),
        bullet("b4", "r1", "Mentored one junior developer"),
        bullet("b5", "r1", "Shipped the invoice portal in March 2021"),
        role("r2", "Kestrel Demo", "Data Analyst", "2016-01", "2019-02"),
        bullet("b6", "r2", "Wrote weekly SQL reports for two teams"),
        bullet("b7", "r2", "Automated the invoice export"),
        skill("py", "Python"), skill("sql", "SQL"), skill("go", "Go"), skill("pg", "PostgreSQL"), skill("dk", "Docker"),
        degree("edu", "B.Sc. Computer Science", "Lakeshore Technical College", "2012", "2016"),
        cert("cert", "Certified Scrum Master", "2018"),
        skill("qs", "Quillscript", chat=True), skill("tf", "Terraform", chat=True),
    ],  # fmt: skip
    # a data analyst
    "da": [
        role("r1", "Halden Demo", "Senior Data Analyst", "2020-06"),
        bullet("b1", "r1", "Built the weekly sales dashboard in Tableau"),
        bullet("b2", "r1", "Reduced manual reporting time from 10 hours to 2 hours a week"),
        bullet("b3", "r1", "Helped define the customer churn metrics"),
        bullet("b4", "r1", "Presented monthly results to a team of eight managers"),
        role("r2", "Marlow Demo", "Reporting Analyst", "2017-02", "2020-05"),
        bullet("b5", "r2", "Maintained 40 SQL reports for the finance team"),
        bullet("b6", "r2", "Cleaned survey data for the 2018 customer study"),
        bullet("b7", "r2", "Supported the migration to a new data warehouse"),
        skill("tb", "Tableau"), skill("sql", "SQL"), skill("xl", "Excel"), skill("py", "Python"),
        skill("st", "Statistics"),
        degree("edu", "M.Sc. Statistics", "Northgate University", "2014", "2016"),
        cert("cert", "Dashboard Design Certificate", "2019"),
        skill("dbt", "dbt", chat=True), skill("af", "Airflow", chat=True),
    ],  # fmt: skip
    # a site reliability engineer
    "sre": [
        role("r1", "Wexford Demo", "Site Reliability Engineer", "2021-04"),
        bullet("b1", "r1", "Ran the on-call rota for a team of six engineers"),
        bullet("b2", "r1", "Cut deployment time from 40 minutes to 12 minutes"),
        bullet("b3", "r1", "Contributed to the incident review process"),
        bullet("b4", "r1", "Moved 15 services to containers in 2022"),
        role("r2", "Larkspur Demo", "Systems Administrator", "2016-09", "2021-03"),
        bullet("b5", "r2", "Managed 120 Linux servers"),
        bullet("b6", "r2", "Wrote backup scripts in Bash"),
        bullet("b7", "r2", "Helped the security team patch servers"),
        skill("lx", "Linux"), skill("dk", "Docker"), skill("k8", "Kubernetes"), skill("sh", "Bash"),
        skill("pm", "Prometheus"),
        degree("edu", "B.Eng. Electrical Engineering", "Ashbourne College", "2011", "2015"),
        cert("cert", "Linux Administration Certificate", "2017"),
        skill("tf", "Terraform", chat=True), skill("an", "Ansible", chat=True),
    ],  # fmt: skip
    # a frontend developer
    "fe": [
        role("r1", "Orrin Demo", "Frontend Developer", "2020-01", "2023-06"),
        bullet("b1", "r1", "Built the account settings pages in React"),
        bullet("b2", "r1", "Improved page load time by 35%"),
        bullet("b3", "r1", "Worked with two designers on the new checkout"),
        bullet("b4", "r1", "Wrote unit tests for 20 components"),
        role("r2", "Tamsin Demo", "Web Designer", "2017-05", "2019-12"),
        bullet("b5", "r2", "Designed landing pages for 12 clients"),
        bullet("b6", "r2", "Helped launch the redesigned site in 2018"),
        skill("js", "JavaScript"), skill("ts", "TypeScript"), skill("rc", "React"), skill("css", "CSS"),
        skill("html", "HTML"),
        degree("edu", "B.A. Graphic Design", "Riverbend College", "2013", "2017"),
        cert("cert", "Accessibility Fundamentals Certificate", "2021"),
        skill("vue", "Vue", chat=True), skill("fg", "Figma", chat=True),
    ],  # fmt: skip
    # a product manager
    "pm": [
        role("r1", "Quillon Demo", "Product Manager", "2019-08"),
        bullet("b1", "r1", "Managed the roadmap for the payments product"),
        bullet("b2", "r1", "Launched the referral feature in 2021"),
        bullet("b3", "r1", "Worked with engineering on the release plan"),
        bullet("b4", "r1", "Grew weekly active users by 18%"),
        role("r2", "Initech Demo", "Associate Product Manager", "2016-06", "2019-07"),
        bullet("b5", "r2", "Wrote requirements for six features"),
        bullet("b6", "r2", "Supported customer interviews"),
        bullet("b7", "r2", "Helped prioritise the backlog"),
        skill("rm", "Roadmapping"), skill("sql", "SQL"), skill("jr", "Jira"), skill("ab", "A/B testing"),
        skill("ur", "User research"),
        degree("edu", "MBA", "Fairhaven Business School", "2014", "2016"),
        cert("cert", "Certified Scrum Product Owner", "2018"),
        skill("am", "Amplitude", chat=True), skill("fg", "Figma", chat=True),
    ],  # fmt: skip
    # a marketing analyst
    "ma": [
        role("r1", "Fenwick Demo", "Marketing Analyst", "2021-02"),
        bullet("b1", "r1", "Tracked results of 25 email campaigns"),
        bullet("b2", "r1", "Raised email open rates by 12%"),
        bullet("b3", "r1", "Supported the brand relaunch in 2022"),
        bullet("b4", "r1", "Built the monthly marketing report in Excel"),
        role("r2", "Soylent Demo", "Marketing Assistant", "2018-07", "2021-01"),
        bullet("b5", "r2", "Scheduled posts for three social channels"),
        bullet("b6", "r2", "Helped organise two trade shows"),
        bullet("b7", "r2", "Wrote product descriptions for the online shop"),
        skill("xl", "Excel"), skill("ga", "Google Analytics"), skill("seo", "SEO"), skill("mc", "Mailchimp"),
        skill("cw", "Copywriting"),
        degree("edu", "B.A. Marketing", "Eastfield University", "2014", "2018"),
        skill("hs", "HubSpot", chat=True), skill("pb", "Power BI", chat=True),
    ],  # fmt: skip
}

PAIRS: list[dict] = [
    # ---- bk: a backend engineer ----------------------------------------
    pair(1, "bk", [
        ("req", "Experience building Python services", "m"), ("req", "PostgreSQL in production", "m"),
        ("req", "Kubernetes and container orchestration", "n"), ("pref", "AWS cloud infrastructure", "n"),
        ("pref", "Mentoring junior engineers", "m"),
    ], [
        reorder(["pg", "py", "sql", "go", "dk"], reqs=[2, 1]),
        remove("b7"),
        rephrase("b1", "Developed Python services for payments", reqs=[1]),
        rephrase("b4", "Mentored one junior developer on Kubernetes", kind="new_skill", reason="new_entity",
                 why="Kubernetes comes from the posting; no fact says the candidate used it."),
        rephrase("b2", "Cut report build time by 45%", kind="inflated_number", reason="changed_number",
                 why="The fact says 30%."),
        rephrase("b3", "Contributed to the checkout migration as Staff Backend Engineer", kind="promoted_title",
                 reason="changed_title", why="The title was Backend Engineer; Staff is a promotion."),
    ]),
    pair(2, "bk", [
        ("req", "Terraform and infrastructure as code", "p"), ("req", "Python or Go for internal tooling", "m"),
        ("req", "CI/CD pipelines", "n"), ("pref", "Docker", "m"), ("pref", "Participating in an on-call rota", "n"),
    ], [
        add("tf", reqs=[1]),
        rephrase("b5", "Delivered the invoice portal in March 2021"),
        add("qs", "Quillscript scripting language", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Quillscript; the edit adds words of its own."),
        rephrase("b6", "Wrote weekly SQL reports for two teams at Brindle Demo", kind="swapped_employer",
                 reason="changed_employer", why="The reports were written at Kestrel Demo, not Brindle Demo."),
        rephrase("b2", "Cut report build time by 30% in 2020", kind="changed_date", reason="changed_date",
                 why="The fact gives no year."),
        reorder(["b2", "b1", "b3", "b4"], kind="reorder_drops_item", reason="no_supporting_fact",
                why="The invoice portal bullet is left out."),
    ]),
    pair(3, "bk", [
        ("req", "5+ years of backend development", "p"), ("req", "Experience designing APIs", "n"),
        ("req", "Strong SQL skills", "m"), ("pref", "Experience with message queues such as Kafka", "n"),
        ("pref", "Technical leadership of small teams", "p"),
    ], [
        rephrase("b6", "Wrote SQL reports every week for two teams", reqs=[3]),
        rephrase("b3", "Led the checkout migration", kind="inflated_scope", reason="not_entailed",
                 why="Contributed to is not led."),
        rephrase("b4", "Managed and mentored a team of junior developers", kind="inflated_scope",
                 reason="not_entailed", why="One mentee is not a team."),
        rephrase("b1", "Built Python services for payments, including message queues with Kafka", kind="new_skill",
                 reason="new_entity", why="Kafka comes from the posting."),
        remove("b7"),
        reorder(["b1", "b3", "b5", "b2", "b4"]),
    ]),
    pair(4, "bk", [
        ("req", "SQL and relational databases", "m"), ("req", "Python scripting", "m"),
        ("req", "Experience with ETL pipelines", "p"), ("pref", "Airflow or similar schedulers", "n"),
    ], [
        rephrase("b6", "Produced weekly SQL reports for two teams", reqs=[1]),
        rephrase("b1", "Built Python services for payments at scale", [], kind="no_support_named",
                 reason="no_supporting_fact", why="No supporting fact is named."),
        rephrase("b2", "Cut report build time by 30% as Senior Backend Engineer", kind="promoted_title",
                 reason="changed_title", why="The title was Backend Engineer."),
        add("qs"),
        rephrase("b4", "Mentored three junior developers", kind="inflated_number", reason="changed_number",
                 why="The fact says one."),
        rephrase("b7", "Automated the invoice export using cron", kind="layer1_gap", reason="not_entailed",
                 why="A lower-case tool that no fact or posting names: only layer 2 can see it."),
    ]),
    pair(5, "bk", [
        ("req", "SQL and reporting", "m"), ("req", "Dashboards in Tableau or similar", "n"), ("pref", "Python", "m"),
        ("pref", "Stakeholder communication", "p"),
    ], [
        reorder(["sql", "py", "pg", "go", "dk"], reqs=[1, 3]),
        reorder(["sql", "py", "pg", "go", "dk", "qs"], kind="reorder_adds_item", reason="new_entity",
                why="Quillscript is not on the skills line; adding it by reorder skips the add check."),
        remove("b4"),
        add("tf", "Terraform (infrastructure as code)", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Terraform."),
        reorder(["b1", "b2", "b3"], kind="reorder_drops_item", reason="no_supporting_fact",
                why="Two bullets are left out."),
    ], rephrase=False),
    # ---- da: a data analyst ----------------------------------------
    pair(6, "da", [
        ("req", "dbt and SQL modelling", "p"), ("req", "Dashboards in Tableau or Looker", "m"),
        ("req", "Experience with Airflow", "n"), ("pref", "Data quality testing", "n"),
        ("pref", "Communicating results to managers", "m"),
    ], [
        add("dbt", reqs=[1]),
        rephrase("b1", "Created a weekly sales dashboard in Tableau", reqs=[2]),
        rephrase("b2", "Reduced manual reporting time from 10 hours to 1 hour a week", kind="inflated_number",
                 reason="changed_number", why="The fact says 2 hours."),
        rephrase("b5", "Maintained 40 SQL reports for the finance team using Looker", kind="new_skill",
                 reason="new_entity", why="Looker comes from the posting."),
        rephrase("b7", "Supported the migration to a new data warehouse at Halden Demo", kind="swapped_employer",
                 reason="changed_employer", why="The migration was at Marlow Demo."),
        reorder(["b2", "b1", "b4", "b3"]),
    ]),
    pair(7, "da", [
        ("req", "3+ years in business intelligence", "p"), ("req", "Advanced Excel", "m"), ("req", "SQL", "m"),
        ("pref", "Data storytelling", "n"), ("pref", "Power BI", "n"),
    ], [
        rephrase("b5", "Looked after 40 SQL reports for the finance team", reqs=[3]),
        rephrase("b3", "Defined the customer churn metrics", kind="inflated_scope", reason="not_entailed",
                 why="Helped define is not defined."),
        rephrase("b6", "Cleaned survey data for the 2019 customer study", kind="changed_date", reason="changed_date",
                 why="The fact says 2018."),
        rephrase("b4", "Presented monthly results to a team of eight managers as Lead Data Analyst",
                 kind="promoted_title", reason="changed_title",
                 why="The title was Senior Data Analyst; Lead Data Analyst is another title."),
        remove("b7"),
        reorder(["sql", "xl", "tb", "py", "st"], reqs=[2, 3]),
    ]),
    pair(8, "da", [
        ("req", "Excel and reporting", "m"), ("req", "Google Analytics", "n"), ("req", "Statistics", "m"),
        ("pref", "Experience with A/B testing", "n"),
    ], [
        rephrase("b2", "Cut manual reporting time from 10 hours to 2 hours a week", reqs=[1]),
        rephrase("b4", "Presented monthly results to a team of eight managers using Google Analytics",
                 kind="new_skill", reason="new_entity", why="Google Analytics comes from the posting."),
        rephrase("b5", "Maintained 400 SQL reports for the finance team", kind="inflated_number",
                 reason="changed_number", why="The fact says 40."),
        remove("b6"),
        add("af"),
    ]),
    pair(9, "da", [
        ("req", "Machine learning in production", "n"), ("req", "Python and statistics", "m"),
        ("req", "Experience mentoring analysts", "n"), ("pref", "Causal inference", "n"),
    ], [
        rephrase("b3", "Helped define customer churn metrics"),
        rephrase("b1", "Owned the weekly sales dashboard in Tableau", kind="inflated_scope", reason="not_entailed",
                 why="Built is not owned."),
        rephrase("b7", "Supported the migration to a new data warehouse and trained analysts on it",
                 kind="layer1_gap", reason="not_entailed",
                 why="The training is a new claim in plain words; no layer-1 rule sees it."),
        rephrase("b5", "Maintained 40 SQL reports for the finance team as Principal Analyst", kind="promoted_title",
                 reason="changed_title", why="The title was Reporting Analyst."),
        reorder(["b7", "b5", "b6"]),
    ]),
    pair(10, "da", [
        ("req", "SQL", "m"), ("req", "Tableau dashboards", "m"), ("pref", "dbt", "n"), ("pref", "Python", "m"),
    ], [
        reorder(["tb", "sql", "py", "xl", "st"], reqs=[1, 2]),
        add("dbt", reqs=[3]),
        reorder(["tb", "sql", "py", "xl", "st", "af"], kind="reorder_adds_item", reason="new_entity",
                why="Airflow is not on the skills line."),
        add("af", "Apache Airflow", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Airflow; Apache is added."),
        remove("b6"),
    ], rephrase=False),
    # ---- sre: a site reliability engineer ----------------------------------------
    pair(11, "sre", [
        ("req", "Production Kubernetes experience", "m"), ("req", "Infrastructure as code with Terraform", "p"),
        ("req", "Observability with Prometheus and Grafana", "p"), ("pref", "AWS or GCP", "n"),
        ("pref", "Incident response leadership", "p"),
    ], [
        add("tf", reqs=[2]),
        reorder(["k8", "pm", "dk", "lx", "sh"], reqs=[1, 3]),
        rephrase("b2", "Cut deployment time from 40 minutes to 12 minutes with Grafana dashboards", kind="new_skill",
                 reason="new_entity", why="Grafana comes from the posting."),
        rephrase("b4", "Moved 15 services to containers in 2021", kind="changed_date", reason="changed_date",
                 why="The fact says 2022."),
        rephrase("b3", "Led the incident review process", kind="inflated_scope", reason="not_entailed",
                 why="Contributed to is not led."),
        rephrase("b1", "Ran the on-call rota for six engineers"),
    ]),
    pair(12, "sre", [
        ("req", "AWS services such as EC2 and S3", "n"), ("req", "Terraform", "p"), ("req", "Linux administration", "m"),
        ("pref", "Python or Bash scripting", "m"), ("pref", "Ansible", "n"),
    ], [
        add("an", reqs=[5]),
        rephrase("b5", "Managed 120 Linux servers on AWS", kind="new_skill", reason="new_entity",
                 why="AWS comes from the posting."),
        rephrase("b6", "Wrote backup scripts in Bash at Wexford Demo", kind="swapped_employer",
                 reason="changed_employer", why="The scripts were written at Larkspur Demo."),
        rephrase("b7", "Helped the security team patch 300 servers", kind="inflated_number", reason="changed_number",
                 why="The fact gives no number."),
        rephrase("b2", "Reduced deployment time from 40 minutes to 12 minutes"),
        remove("b4"),
    ]),
    pair(13, "sre", [
        ("req", "CI/CD pipelines", "n"), ("req", "Docker and Kubernetes", "m"), ("req", "Bash scripting", "m"),
        ("pref", "Terraform", "p"), ("pref", "Mentoring", "n"),
    ], [
        reorder(["b2", "b4", "b1", "b3"]),
        rephrase("b1", "Ran the on-call rota for a team of six engineers as Engineering Manager",
                 kind="promoted_title", reason="changed_title", why="The title was Site Reliability Engineer."),
        rephrase("b6", "Wrote backup scripts in Bash and Python", [], kind="no_support_named",
                 reason="no_supporting_fact", why="No supporting fact is named, and Python is not in the fact."),
        rephrase("b7", "Helped the security team with server patching"),
        add("tf", reqs=[4]),
    ]),
    pair(14, "sre", [
        ("req", "Leading incident response", "p"), ("req", "Designing reliable distributed systems", "n"),
        ("req", "Kubernetes", "m"), ("pref", "Mentoring engineers", "n"),
    ], [
        rephrase("b7", "Led the security team's patching of servers", kind="inflated_scope", reason="not_entailed",
                 why="Helped is not led."),
        rephrase("b2", "Cut deployment time from 40 minutes to 12 minutes by designing distributed systems",
                 kind="new_skill", reason="new_entity",
                 why="Designing distributed systems comes from the posting; the fact says nothing of it."),
        reorder(["k8", "lx", "dk", "pm", "sh"], reqs=[3]),
        rephrase("b4", "Moved 15 services to containers in 2020", kind="changed_date", reason="changed_date",
                 why="The fact says 2022."),
        remove("b6"),
        rephrase("b5", "Administered 120 Linux servers"),
    ]),
    pair(15, "sre", [
        ("req", "Linux and Bash", "m"), ("req", "Monitoring with Prometheus", "m"), ("pref", "Terraform", "n"),
        ("pref", "Ansible", "n"),
    ], [
        reorder(["lx", "sh", "pm", "k8", "dk"], reqs=[1, 2]),
        reorder(["lx", "sh", "pm", "k8", "dk", "tf"], kind="reorder_adds_item", reason="new_entity",
                why="Terraform is not on the skills line."),
        add("an", "Ansible automation", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Ansible."),
        add("tf", reqs=[3]),
        remove("b3"),
        reorder(["b7", "b6"], kind="reorder_drops_item", reason="no_supporting_fact",
                why="The Linux servers bullet is left out."),
    ], rephrase=False),
    # ---- fe: a frontend developer ----------------------------------------
    pair(16, "fe", [
        ("req", "React and TypeScript", "m"), ("req", "Testing with Jest", "p"), ("req", "Performance optimisation", "m"),
        ("pref", "Vue", "n"), ("pref", "GraphQL", "n"),
    ], [
        add("vue", reqs=[4]),
        rephrase("b2", "Improved page load time by 35 percent", reqs=[3]),
        rephrase("b4", "Wrote unit tests for 20 components with Jest", kind="new_skill", reason="new_entity",
                 why="Jest comes from the posting."),
        rephrase("b3", "Worked with five designers on the new checkout", kind="inflated_number",
                 reason="changed_number", why="The fact says two."),
        rephrase("b6", "Helped launch the redesigned site in 2019", kind="changed_date", reason="changed_date",
                 why="The fact says 2018."),
        reorder(["rc", "ts", "js", "css", "html"], reqs=[1]),
    ]),
    pair(17, "fe", [
        ("req", "Deep React experience", "m"), ("req", "Leading frontend architecture", "n"),
        ("req", "Accessibility", "m"), ("pref", "Mentoring developers", "n"),
    ], [
        rephrase("b3", "Led two designers on the new checkout", kind="inflated_scope", reason="not_entailed",
                 why="Worked with is not led."),
        rephrase("b1", "Built the account settings pages in React as Lead Frontend Developer", kind="promoted_title",
                 reason="changed_title", why="The title was Frontend Developer."),
        rephrase("b4", "Wrote unit tests covering 20 components"),
        remove("b5"),
        reorder(["b2", "b1", "b4", "b3"], reqs=[1]),
    ]),
    pair(18, "fe", [
        ("req", "CSS and HTML", "m"), ("req", "Design systems", "p"), ("req", "Figma", "n"), ("pref", "Animation", "n"),
    ], [
        add("fg", reqs=[3]),
        rephrase("b5", "Designed landing pages for 12 clients at Orrin Demo", kind="swapped_employer",
                 reason="changed_employer", why="The landing pages were made at Tamsin Demo."),
        rephrase("b1", "Built the account settings pages in React from Figma designs", kind="new_skill",
                 reason="new_entity",
                 why="'designs' comes from the posting's Design systems, and no fact ties Figma to this work."),
        rephrase("b2", "Improved page load time by 35% by optimising animation", kind="new_skill",
                 reason="new_entity", why="Animation comes from the posting."),
        rephrase("b3", "Collaborated with two designers on the new checkout"),
        reorder(["css", "html", "rc", "ts", "js"], reqs=[1]),
    ]),
    pair(19, "fe", [
        ("req", "HTML, CSS and JavaScript", "m"), ("req", "Responsive design", "p"),
        ("req", "Client communication", "n"), ("pref", "WordPress", "n"),
    ], [
        rephrase("b5", "Designed landing pages for 12 clients in WordPress", kind="new_skill", reason="new_entity",
                 why="WordPress comes from the posting."),
        rephrase("b4", "Wrote unit tests for 200 components", kind="inflated_number", reason="changed_number",
                 why="The fact says 20."),
        rephrase("b1", "Created the account settings pages in React"),
        rephrase("b6", "Helped launch the redesigned site in 2018 as Head of Web", kind="promoted_title",
                 reason="changed_title", why="The title was Web Designer."),
        remove("b3"),
        reorder(["b6", "b5"]),
    ]),
    pair(20, "fe", [
        ("req", "React", "m"), ("req", "TypeScript", "m"), ("pref", "Vue", "n"), ("pref", "Figma", "n"),
    ], [
        reorder(["rc", "ts", "js", "html", "css"], reqs=[1, 2]),
        add("vue", reqs=[3]),
        add("fg", "Figma design tool", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Figma."),
        reorder(["rc", "ts", "js", "css", "html", "vue"], kind="reorder_adds_item", reason="new_entity",
                why="Vue is not on the skills line."),
        reorder(["b1", "b2", "b3"], kind="reorder_drops_item", reason="no_supporting_fact",
                why="The unit tests bullet is left out."),
    ], rephrase=False),
    # ---- pm: a product manager ----------------------------------------
    pair(21, "pm", [
        ("req", "Owning a product roadmap", "m"), ("req", "Data-informed decisions using SQL and Amplitude", "p"),
        ("req", "Leading cross-functional teams", "p"), ("pref", "B2B SaaS experience", "n"),
    ], [
        add("am", reqs=[2]),
        rephrase("b3", "Led engineering on the release plan", kind="inflated_scope", reason="not_entailed",
                 why="Worked with is not led."),
        rephrase("b4", "Grew weekly active users by 80%", kind="inflated_number", reason="changed_number",
                 why="The fact says 18%."),
        rephrase("b2", "Shipped the referral feature in 2021"),
        rephrase("b1", "Managed the roadmap for the payments product as Director of Product", kind="promoted_title",
                 reason="changed_title", why="The title was Product Manager."),
        remove("b6"),
    ]),
    pair(22, "pm", [
        ("req", "A/B testing and experimentation", "m"), ("req", "SQL for analysis", "m"),
        ("req", "Experience with Mixpanel or Amplitude", "n"), ("pref", "Lifecycle marketing", "n"),
    ], [
        rephrase("b4", "Grew weekly active users by 18% with Mixpanel experiments", kind="new_skill",
                 reason="new_entity", why="Mixpanel comes from the posting."),
        rephrase("b2", "Launched the referral feature in 2020", kind="changed_date", reason="changed_date",
                 why="The fact says 2021."),
        reorder(["ab", "sql", "rm", "ur", "jr"], reqs=[1, 2]),
        rephrase("b3", "Partnered with engineering on the release plan"),
        rephrase("b7", "Helped prioritise the backlog using customer revenue data", kind="layer1_gap",
                 reason="not_entailed", why="The revenue data is a new claim in plain words."),
        add("fg"),
    ]),
    pair(23, "pm", [
        ("req", "Writing clear requirements", "m"), ("req", "Customer interviews", "m"), ("pref", "SQL basics", "m"),
        ("pref", "Scrum or Kanban", "p"),
    ], [
        rephrase("b6", "Supported customer interviews at Quillon Demo", kind="swapped_employer",
                 reason="changed_employer", why="The interviews were at Initech Demo."),
        rephrase("b5", "Wrote requirements for six features as Senior Associate", kind="promoted_title",
                 reason="changed_title", why="The title was Associate Product Manager."),
        rephrase("b7", "Helped to prioritise the backlog"),
        reorder(["b6", "b5", "b7"], reqs=[2]),
        rephrase("b3", "Worked with three engineering teams on the release plan", kind="inflated_number",
                 reason="changed_number", why="The fact gives no number."),
    ]),
    pair(24, "pm", [
        ("req", "Working closely with engineers", "m"), ("req", "API products", "n"),
        ("req", "Jira and agile delivery", "m"), ("pref", "Machine learning basics", "n"),
    ], [
        rephrase("b1", "Managed the roadmap for the payments product, including API strategy", kind="new_skill",
                 reason="new_entity", why="API comes from the posting."),
        rephrase("b6", "Ran customer interviews", kind="inflated_scope", reason="not_entailed",
                 why="Supported is not ran."),
        rephrase("b4", "Increased weekly active users by 18%"),
        add("am"),
        reorder(["b3", "b1", "b4", "b2"], reqs=[1]),
        rephrase("b2", "Launched the referral feature in March 2021", kind="changed_date", reason="changed_date",
                 why="The fact gives the year, not the month."),
    ]),
    pair(25, "pm", [
        ("req", "Roadmapping", "m"), ("req", "User research", "m"), ("pref", "Amplitude", "n"), ("pref", "Figma", "n"),
    ], [
        reorder(["rm", "ur", "ab", "sql", "jr"], reqs=[1, 2]),
        add("am", "Amplitude analytics", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word Amplitude."),
        add("fg", reqs=[4]),
        reorder(["rm", "ur", "ab", "sql", "jr", "fg"], kind="reorder_adds_item", reason="new_entity",
                why="Figma is not on the skills line."),
        remove("b7"),
    ], rephrase=False),
    # ---- ma: a marketing analyst ----------------------------------------
    pair(26, "ma", [
        ("req", "Google Analytics and reporting", "m"), ("req", "Email marketing metrics", "m"), ("req", "SQL", "n"),
        ("pref", "Power BI or Tableau", "n"),
    ], [
        add("pb", reqs=[4]),
        rephrase("b4", "Built the monthly marketing report in Excel and SQL", kind="new_skill", reason="new_entity",
                 why="SQL comes from the posting."),
        rephrase("b2", "Raised email open rates by 21%", kind="inflated_number", reason="changed_number",
                 why="The fact says 12%."),
        rephrase("b1", "Monitored results of 25 email campaigns", reqs=[2]),
        rephrase("b3", "Supported the brand relaunch in 2021", kind="changed_date", reason="changed_date",
                 why="The fact says 2022."),
        reorder(["ga", "xl", "mc", "seo", "cw"], reqs=[1]),
    ]),
    pair(27, "ma", [
        ("req", "Leading a marketing team", "n"), ("req", "Campaign strategy", "p"), ("req", "Budget ownership", "n"),
        ("pref", "HubSpot", "n"),
    ], [
        rephrase("b1", "Tracked results of 25 email campaigns as Marketing Manager", kind="promoted_title",
                 reason="changed_title", why="The title was Marketing Analyst."),
        rephrase("b6", "Organised two trade shows", kind="inflated_scope", reason="not_entailed",
                 why="Helped organise is not organised."),
        add("hs", reqs=[4]),
        rephrase("b7", "Wrote product descriptions for the online shop at Fenwick Demo", kind="swapped_employer",
                 reason="changed_employer", why="The descriptions were written at Soylent Demo."),
        remove("b5"),
        rephrase("b4", "Created the monthly marketing report in Excel"),
    ]),
    pair(28, "ma", [
        ("req", "Copywriting", "m"), ("req", "SEO", "m"), ("req", "Content calendars", "n"),
        ("pref", "Canva or Figma", "n"),
    ], [
        rephrase("b7", "Wrote descriptions of products for the online shop", reqs=[1]),
        rephrase("b5", "Scheduled posts for three social channels in Canva", kind="new_skill", reason="new_entity",
                 why="Canva comes from the posting."),
        rephrase("b6", "Helped organise twelve trade shows", kind="inflated_number", reason="changed_number",
                 why="The fact says two."),
        reorder(["b7", "b5", "b6"]),
        remove("b2"),
        rephrase("b1", "Tracked results of 25 email campaigns and reported them to leadership", kind="layer1_gap",
                 reason="not_entailed", why="The reporting line is a new claim in plain words."),
    ]),
    pair(29, "ma", [
        ("req", "Paid acquisition", "n"), ("req", "Funnel analysis", "p"), ("req", "Experimentation", "n"),
        ("pref", "Power BI", "n"),
    ], [
        rephrase("b2", "Raised email open rates by 12% as Head of Growth", kind="promoted_title",
                 reason="changed_title", why="The title was Marketing Analyst."),
        add("pb", reqs=[4]),
        rephrase("b6", "Helped organise two trade shows in 2019", kind="changed_date", reason="changed_date",
                 why="The fact gives no year."),
        rephrase("b1", "Tracked the results of 25 email campaigns"),
        rephrase("b4", "Owned the monthly marketing report in Excel", kind="inflated_scope", reason="not_entailed",
                 why="Built is not owned."),
        reorder(["ga", "mc", "xl", "seo", "cw"], reqs=[2]),
    ]),
    pair(30, "ma", [
        ("req", "Excel", "m"), ("req", "Google Analytics", "m"), ("pref", "HubSpot", "n"), ("pref", "Power BI", "n"),
    ], [
        reorder(["xl", "seo", "ga", "mc", "cw"], reqs=[1, 2]),
        add("hs", "HubSpot CRM", kind="paraphrased_add", reason="no_supporting_fact",
            why="The fact is the word HubSpot."),
        reorder(["xl", "ga", "seo", "mc", "cw", "pb"], kind="reorder_adds_item", reason="new_entity",
                why="Power BI is not on the skills line."),
        add("pb", reqs=[4]),
        reorder(["b5", "b6"], kind="reorder_drops_item", reason="no_supporting_fact",
                why="The product descriptions bullet is left out."),
        remove("b6"),
    ], rephrase=False),
]  # fmt: skip
