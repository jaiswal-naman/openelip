"""The independent constraint checker (MAT-01, invariant 1): which hard constraints a job breaks, and which fields it
leaves unstated.

Written from the rules of DATA_MODEL 4.1 and the column meanings of 3.4 and 3.5, as a second implementation beside the
SQL functions constraint_violations() and unverified_fields(). The property suite (plan 04-04) compares the two over
10,000 generated cases, and a disagreement is fixed on the side that contradicts the documented rule.

The rule for unknown values: a constraint removes a job only when a STATED field contradicts it. A field the posting
leaves unstated passes here and is reported by unverified(). A constraint the person did not set (None) is never
reported. Where the SQL would compare with NULL, which is neither true nor false, nothing is reported either.

Pure functions: no I/O, nothing logged. Both return the enum values (constraint_kind, unverified_flag) in enum order.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class JobFacts:
    """The columns of jobs that the rules read, as the table stores them. The defaults are the table's own: an empty
    arrangement list, and 'unknown' for employment type, seniority and visa sponsorship, mean "not stated"."""

    company_name: str
    country: str | None = None  # ISO 3166-1 alpha-2, None when the posting does not say
    city: str | None = None  # canonical name as ingest stores it
    work_arrangements: Sequence[str] = ()
    employment_type: str = "unknown"
    seniority: str = "unknown"
    salary_min: int | None = None  # read by no rule: the floor is compared with the top of the range
    salary_max: int | None = None
    salary_currency: str | None = None
    visa_sponsorship: str = "unknown"


@dataclass(frozen=True)
class Constraints:
    """The fields of compiled_constraints (DATA_MODEL 4.1). None means no constraint of that kind. Cities are lower
    case canonical names; excluded companies are company_key() values."""

    countries: Sequence[str] | None = None
    cities: Sequence[str] | None = None
    arrangements: Sequence[str] | None = None
    authorized_countries: Sequence[str] | None = None
    salary_floor: int | None = None
    salary_currency: str | None = None
    employment_types: Sequence[str] | None = None
    seniorities: Sequence[str] | None = None
    excluded_company_keys: Sequence[str] | None = None


# The same words DATA_MODEL 3.2 strips, each only as a whole word.
_LEGAL_SUFFIX = re.compile(
    r"\b(?:incorporated|inc|llc|llp|ltd|limited|corp|corporation|co|company|gmbh|plc|pvt|private)\b"
)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def company_key(name: str | None) -> str:
    """A company name reduced to a comparison key: lower case, legal suffixes removed, letters and digits only.
    "ACME, Inc." and "Acme Incorporated" are both "acme". A missing name is the empty key."""
    return _NOT_ALNUM.sub("", _LEGAL_SUFFIX.sub("", (name or "").lower()))


def _breaks_location(job: JobFacts, c: Constraints) -> bool:
    if c.countries is not None and job.country is not None and job.country not in c.countries:
        return True
    # A remote job can be done from any city, so the cities do not apply to it (the countries still do).
    return (
        c.cities is not None
        and job.city is not None
        and "remote" not in job.work_arrangements
        and job.city.lower() not in c.cities
    )


def _breaks_remote_policy(job: JobFacts, c: Constraints) -> bool:
    return (
        c.arrangements is not None
        and bool(job.work_arrangements)
        and not set(job.work_arrangements) & set(c.arrangements)
    )


def _breaks_work_authorization(job: JobFacts, c: Constraints) -> bool:
    # Only an explicit offer of sponsorship lets a job outside the authorized countries through; "unknown" does not.
    return (
        c.authorized_countries is not None
        and job.country is not None
        and job.country not in c.authorized_countries
        and job.visa_sponsorship != "offered"
    )


def _breaks_salary_floor(job: JobFacts, c: Constraints) -> bool:
    return (
        c.salary_floor is not None
        and job.salary_max is not None
        and job.salary_currency is not None
        and job.salary_currency == c.salary_currency
        and job.salary_max < c.salary_floor
    )


def _breaks_employment_type(job: JobFacts, c: Constraints) -> bool:
    return (
        c.employment_types is not None
        and job.employment_type != "unknown"
        and job.employment_type not in c.employment_types
    )


def _breaks_seniority(job: JobFacts, c: Constraints) -> bool:
    return c.seniorities is not None and job.seniority != "unknown" and job.seniority not in c.seniorities


def _breaks_excluded_company(job: JobFacts, c: Constraints) -> bool:
    return c.excluded_company_keys is not None and company_key(job.company_name) in c.excluded_company_keys


_VIOLATIONS: tuple[tuple[str, Callable[[JobFacts, Constraints], bool]], ...] = (
    ("location", _breaks_location),
    ("remote_policy", _breaks_remote_policy),
    ("work_authorization", _breaks_work_authorization),
    ("salary_floor", _breaks_salary_floor),
    ("employment_type", _breaks_employment_type),
    ("seniority", _breaks_seniority),
    ("excluded_company", _breaks_excluded_company),
)


def violations(job: JobFacts, c: Constraints) -> list[str]:
    """The constraint kinds the job breaks, in enum order. Empty means eligible."""
    return [kind for kind, breaks in _VIOLATIONS if breaks(job, c)]


def _location_not_stated(job: JobFacts, c: Constraints) -> bool:
    return (c.countries is not None or c.authorized_countries is not None) and job.country is None


def _arrangement_not_stated(job: JobFacts, c: Constraints) -> bool:
    return c.arrangements is not None and not job.work_arrangements


def _arrangement_ambiguous(job: JobFacts, c: Constraints) -> bool:
    # The posting permits something the person does not accept as well as something they do (or only the former:
    # the SQL reports that too, and a removed job's flags are never shown).
    return (
        c.arrangements is not None
        and bool(job.work_arrangements)
        and not set(job.work_arrangements) <= set(c.arrangements)
    )


def _salary_not_stated(job: JobFacts, c: Constraints) -> bool:
    return c.salary_floor is not None and job.salary_max is None


def _salary_currency_differs(job: JobFacts, c: Constraints) -> bool:
    return (
        c.salary_floor is not None
        and job.salary_max is not None
        and job.salary_currency is not None
        and c.salary_currency is not None
        and job.salary_currency != c.salary_currency
    )


def _employment_type_not_stated(job: JobFacts, c: Constraints) -> bool:
    return c.employment_types is not None and job.employment_type == "unknown"


def _seniority_not_stated(job: JobFacts, c: Constraints) -> bool:
    return c.seniorities is not None and job.seniority == "unknown"


_UNVERIFIED: tuple[tuple[str, Callable[[JobFacts, Constraints], bool]], ...] = (
    ("location_not_stated", _location_not_stated),
    ("arrangement_not_stated", _arrangement_not_stated),
    ("arrangement_ambiguous", _arrangement_ambiguous),
    ("salary_not_stated", _salary_not_stated),
    ("salary_currency_differs", _salary_currency_differs),
    ("employment_type_not_stated", _employment_type_not_stated),
    ("seniority_not_stated", _seniority_not_stated),
)


def unverified(job: JobFacts, c: Constraints) -> list[str]:
    """The constrained fields the job leaves unstated or ambiguous, in enum order. Shown beside the match."""
    return [flag for flag, applies in _UNVERIFIED if applies(job, c)]
