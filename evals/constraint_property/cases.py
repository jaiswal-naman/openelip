"""Invented jobs and constraint sets for the constraint_property suite (plan 04-04, MAT-01).

generate(seed) is a pure function of its seed: it draws only from random.Random(seed), so the same seed gives the same
cases on every machine and a failing pair can be reproduced. There is no property-testing library (INDEX decision 13):
small value spaces, hit from both sides thousands of times, are all the rules of DATA_MODEL 4.1 need. Every company,
title and place here is invented or generic; nothing comes from a real posting or a real person.

A job is a dict with the columns of `jobs` that the rules read. A constraint set is {"key", "constraints"}, where
"constraints" holds one entry for each kind of hard constraint the person set, in the shape of the `constraints` table's
typed columns (a kind that is absent is a constraint the person never set).
"""

import random
from dataclasses import dataclass
from typing import Any

SEED = 20261123  # the gate's seed (a date, so it is plainly arbitrary); the tests try others too
N_JOBS, N_SETS = 240, 60  # 14,400 pairs: more than the 10,000 that MAT-01 asks for

KINDS = (
    "location",
    "remote_policy",
    "work_authorization",
    "salary_floor",
    "employment_type",
    "seniority",
    "excluded_company",
)
COUNTRIES = ("IN", "US", "GB", "DE", "SG", "CA")
CITIES = {
    "IN": ("Bengaluru", "Pune", "Hyderabad"),
    "US": ("Austin", "Denver", "Boston"),
    "GB": ("London", "Leeds"),
    "DE": ("Berlin", "München"),
    "SG": ("Singapore",),
    "CA": ("Toronto", "Vancouver"),
}
ALL_CITIES = tuple(city for cities in CITIES.values() for city in cities)
ARRANGEMENTS = ("onsite", "hybrid", "remote")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary")
SENIORITIES = ("intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive")
VISA_SPONSORSHIP = ("offered", "not_offered", "unknown")
CURRENCIES = ("INR", "USD", "EUR")
FLOORS = (1_000_000, 2_000_000, 3_000_000)
# Pay at, just under and just over every floor (a job paying exactly the floor is not below it), and between them.
PAY_POINTS = tuple(sorted({floor + step for floor in FLOORS for step in (-1, 0, 1)} | {400_000, 1_500_000, 4_500_000}))

# Invented employers. The constraint sets exclude some of them; jobs carry them exactly, written another way (the same
# company: case, punctuation or a legal suffix changed), or as a different company with a similar name.
EMPLOYERS = ("Initech Demo", "Veltrix Labs", "Quillfeather Works", "Halden Demo", "Corvid Demo", "Zoë Demo", "Café Corvid")
OTHER_EMPLOYERS = ("Brightline Demo", "Northwind Demo", "Hooli Demo", "Tessera Demo", "Plover Works")
LEGAL_SUFFIXES = (
    "Inc",
    "Inc.",
    "LLC",
    "Ltd",
    "Ltd.",
    "Limited",
    "Corp.",
    "Corporation",
    "Co.",
    "Company",
    "GmbH",
    "PLC",
    "Pvt. Ltd.",
    "Private Limited",
    "Incorporated",
    "LLP",
)


@dataclass(frozen=True)
class Cases:
    seed: int
    jobs: tuple[dict[str, Any], ...]
    constraint_sets: tuple[dict[str, Any], ...]

    @property
    def pairs(self) -> int:
        return len(self.jobs) * len(self.constraint_sets)


def spelling(rng: random.Random, name: str) -> str:
    """The same company written another way: its case, its punctuation or a legal suffix changed."""
    how = rng.randrange(7)
    if how == 0:
        return name.upper()
    if how == 1:
        return name.lower()
    if how == 2:
        return f"{name}, {rng.choice(LEGAL_SUFFIXES)}"
    if how == 3:
        return f"{name} {rng.choice(LEGAL_SUFFIXES)}"
    if how == 4:
        return name.replace(" ", "-")
    if how == 5:
        return name.replace(" ", "")
    return f"{name}."


def near_miss(rng: random.Random, name: str) -> str:
    """A different company whose name looks like this one's."""
    first, *rest = name.split()
    how = rng.randrange(4)
    if how == 0:
        return f"{name}s"
    if how == 1:
        return " ".join([f"{first}nologies", *rest])
    if how == 2:
        return " ".join([*rest, first])
    return f"The {name}"


def company(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.30:
        return rng.choice(EMPLOYERS)
    if roll < 0.55:
        return spelling(rng, rng.choice(EMPLOYERS))
    if roll < 0.65:
        return near_miss(rng, rng.choice(EMPLOYERS))
    return rng.choice(OTHER_EMPLOYERS)


def place(rng: random.Random) -> tuple[str | None, str | None]:
    """A country and a city, each stated or not. A city is usually one of the country's, and now and then written as a
    feed might (all capitals, all lower case)."""
    country = None if rng.random() < 0.15 else rng.choice(COUNTRIES)
    pool = CITIES[country] if country and rng.random() < 0.9 else ALL_CITIES
    city = None if rng.random() < 0.25 else rng.choice(pool)
    if city and rng.random() < 0.3:
        city = rng.choice((city.upper(), city.lower()))
    return country, city


def arrangements(rng: random.Random) -> list[str]:
    size = rng.choices((0, 1, 2, 3), weights=(2, 5, 3, 1))[0]  # none: the posting does not say
    return sorted(rng.sample(ARRANGEMENTS, size), key=ARRANGEMENTS.index)


def pay(rng: random.Random) -> dict[str, Any]:
    roll, currency = rng.random(), rng.choice(CURRENCIES)
    if roll < 0.30:  # nothing stated, and now and then a currency with no amount
        return {"salary_min": None, "salary_max": None, "salary_currency": rng.choice((None, None, currency))}
    top = rng.choice(PAY_POINTS)
    if roll < 0.75:
        return {"salary_min": top * 4 // 5, "salary_max": top, "salary_currency": currency}
    if roll < 0.85:  # only a minimum: the filter reads the top of the range, so the pay counts as not stated
        return {"salary_min": top, "salary_max": None, "salary_currency": currency}
    return {"salary_min": None, "salary_max": top, "salary_currency": currency}


def job(rng: random.Random, n: int) -> dict[str, Any]:
    country, city = place(rng)
    return {
        "key": f"job-{n:03d}",
        "company_name": company(rng),
        "country": country,
        "city": city,
        "work_arrangements": arrangements(rng),
        "employment_type": "unknown" if rng.random() < 0.2 else rng.choice(EMPLOYMENT_TYPES),
        "seniority": "unknown" if rng.random() < 0.25 else rng.choice(SENIORITIES),
        **pay(rng),
        "visa_sponsorship": rng.choice(VISA_SPONSORSHIP),
    }


def constraint(rng: random.Random, kind: str) -> dict[str, Any]:
    """The typed value of one kind of hard constraint, as the `constraints` table holds it."""
    if kind == "location":
        cities = None if rng.random() < 0.5 else [c.lower() for c in rng.sample(ALL_CITIES, rng.randint(1, 3))]
        return {"country_codes": rng.sample(COUNTRIES, rng.randint(1, 3)), "cities": cities}
    if kind == "remote_policy":
        return {"arrangements": sorted(rng.sample(ARRANGEMENTS, rng.randint(1, 3)), key=ARRANGEMENTS.index)}
    if kind == "work_authorization":
        return {"country_codes": rng.sample(COUNTRIES, rng.randint(1, 3))}
    if kind == "salary_floor":
        return {"salary_floor": {"amount": rng.choice(FLOORS), "currency": rng.choice(CURRENCIES)}}
    if kind == "employment_type":
        return {"employment_types": rng.sample(EMPLOYMENT_TYPES, rng.randint(1, 3))}
    if kind == "seniority":
        return {"seniorities": rng.sample(SENIORITIES, rng.randint(1, 4))}
    names = rng.sample(EMPLOYERS, rng.randint(1, 3))  # as the person typed them: exactly, or written another way
    return {"company_names": [name if rng.random() < 0.5 else spelling(rng, name) for name in names]}


def constraint_set(rng: random.Random, n: int) -> dict[str, Any]:
    """Set 0 holds no constraint, sets 1 to 7 hold one kind each (a rule is tested with nothing else to hide behind),
    set 8 holds all seven, and the rest are random subsets."""
    if n == 0:
        kinds: tuple[str, ...] = ()
    elif n <= len(KINDS):
        kinds = (KINDS[n - 1],)
    elif n == len(KINDS) + 1:
        kinds = KINDS
    else:
        kinds = tuple(kind for kind in KINDS if rng.random() < 0.5)
    return {"key": f"cs-{n:03d}", "constraints": {kind: constraint(rng, kind) for kind in kinds}}


def generate(seed: int = SEED) -> Cases:
    rng = random.Random(seed)
    jobs = tuple(job(rng, n) for n in range(N_JOBS))
    return Cases(seed, jobs, tuple(constraint_set(rng, n) for n in range(N_SETS)))
