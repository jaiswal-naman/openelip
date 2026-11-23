"""The independent constraint checker (MAT-01): which hard constraints a job breaks, and which fields it leaves unstated.

The expectations are the rules of DATA_MODEL 4.1 written out as cases. Plan 04-04 compares the checker with the SQL
functions over 10,000 generated cases; these are the hand-made ones that say what each rule means.
"""

import pytest

from app.match.checker import Constraints, JobFacts, company_key, unverified, violations


def job(**fields):
    return JobFacts(**{"company_name": "Initech Demo", **fields})


# ---- location ----------------------------------------


def test_a_job_in_another_country_than_the_constraint_allows_breaks_location():
    assert violations(job(country="DE"), Constraints(countries=("IN",))) == ["location"]


def test_a_job_in_an_allowed_country_passes():
    assert violations(job(country="IN"), Constraints(countries=("IN", "US"))) == []


def test_a_job_that_names_no_country_passes_and_is_reported_as_unverified():
    c = Constraints(countries=("IN",))
    assert violations(job(), c) == []
    assert unverified(job(), c) == ["location_not_stated"]


def test_a_city_the_person_did_not_list_breaks_location_even_in_an_allowed_country():
    c = Constraints(countries=("IN",), cities=("pune",))
    assert violations(job(country="IN", city="Mumbai"), c) == ["location"]
    assert violations(job(country="IN", city="Pune"), c) == []  # the posting's city is compared in lower case


def test_a_remote_job_is_not_held_to_the_cities_but_is_still_held_to_the_countries():
    c = Constraints(countries=("IN",), cities=("pune",))
    assert violations(job(country="IN", city="Mumbai", work_arrangements=("remote",)), c) == []
    assert violations(job(country="DE", city="Berlin", work_arrangements=("remote",)), c) == ["location"]


def test_a_job_with_a_country_and_no_city_passes_the_city_rule():
    assert violations(job(country="IN"), Constraints(cities=("pune",))) == []


def test_a_constraint_with_cities_alone_checks_the_city_of_a_job_whose_country_is_unknown():
    assert violations(job(city="Berlin"), Constraints(cities=("pune",))) == ["location"]


# ---- remote policy ----------------------------------------


def test_a_job_that_permits_only_onsite_work_breaks_a_remote_only_constraint():
    assert violations(job(work_arrangements=("onsite",)), Constraints(arrangements=("remote",))) == ["remote_policy"]


def test_one_shared_arrangement_is_enough():
    c = Constraints(arrangements=("remote",))
    assert violations(job(work_arrangements=("hybrid", "remote")), c) == []


def test_a_job_that_states_no_arrangement_passes_and_is_reported_as_unverified():
    c = Constraints(arrangements=("remote",))
    assert violations(job(), c) == []
    assert unverified(job(), c) == ["arrangement_not_stated"]


def test_a_job_that_permits_more_than_the_person_accepts_is_flagged_as_ambiguous_not_removed():
    c = Constraints(arrangements=("remote",))
    both = job(work_arrangements=("onsite", "remote"))
    assert violations(both, c) == []
    assert unverified(both, c) == ["arrangement_ambiguous"]
    assert unverified(job(work_arrangements=("remote",)), c) == []  # nothing left open


def test_the_ambiguity_flag_is_reported_for_a_removed_job_too_as_the_sql_does():
    c = Constraints(arrangements=("remote",))
    assert unverified(job(work_arrangements=("onsite",)), c) == ["arrangement_ambiguous"]


# ---- work authorization ----------------------------------------


@pytest.mark.parametrize(
    ("visa", "expected"), [("offered", []), ("not_offered", ["work_authorization"]), ("unknown", ["work_authorization"])]
)
def test_a_job_outside_the_authorized_countries_passes_only_when_it_offers_sponsorship(visa, expected):
    c = Constraints(authorized_countries=("IN",))
    assert violations(job(country="DE", visa_sponsorship=visa), c) == expected


def test_a_job_in_an_authorized_country_passes_whatever_it_says_about_visas():
    c = Constraints(authorized_countries=("IN",))
    assert violations(job(country="IN", visa_sponsorship="not_offered"), c) == []


def test_authorization_with_no_country_on_the_job_passes_and_is_reported_as_unverified():
    c = Constraints(authorized_countries=("IN",))
    assert violations(job(), c) == []
    assert unverified(job(), c) == ["location_not_stated"]


# ---- salary floor ----------------------------------------


def floor(amount=100_000, currency="USD"):
    return Constraints(salary_floor=amount, salary_currency=currency)


def test_a_maximum_below_the_floor_in_the_floors_currency_breaks_it():
    assert violations(job(salary_max=90_000, salary_currency="USD"), floor()) == ["salary_floor"]


def test_a_maximum_at_the_floor_passes():
    assert violations(job(salary_max=100_000, salary_currency="USD"), floor()) == []


def test_only_the_top_of_the_range_decides_so_a_low_minimum_does_not_remove_a_job():
    assert violations(job(salary_min=60_000, salary_max=120_000, salary_currency="USD"), floor()) == []


def test_pay_in_another_currency_passes_and_is_reported_as_unverified():
    posting = job(salary_max=10, salary_currency="EUR")
    assert violations(posting, floor()) == []
    assert unverified(posting, floor()) == ["salary_currency_differs"]


def test_pay_that_is_not_stated_passes_and_is_reported_as_unverified():
    assert violations(job(), floor()) == []
    assert unverified(job(), floor()) == ["salary_not_stated"]
    only_a_minimum = job(salary_min=10, salary_currency="USD")  # a minimum alone says nothing about the top
    assert violations(only_a_minimum, floor()) == []
    assert unverified(only_a_minimum, floor()) == ["salary_not_stated"]


def test_a_stated_maximum_with_no_currency_is_neither_removed_nor_flagged():
    posting = job(salary_max=10)  # the table's CHECK forbids it; the SQL gives NULL here, so nothing is reported
    assert violations(posting, floor()) == [] and unverified(posting, floor()) == []


# ---- employment type and seniority ----------------------------------------


def test_a_stated_employment_type_the_person_did_not_choose_breaks_the_constraint():
    c = Constraints(employment_types=("full_time",))
    assert violations(job(employment_type="contract"), c) == ["employment_type"]
    assert violations(job(employment_type="full_time"), c) == []


def test_an_unknown_employment_type_passes_and_is_reported_as_unverified():
    c = Constraints(employment_types=("full_time",))
    assert violations(job(employment_type="unknown"), c) == []
    assert unverified(job(employment_type="unknown"), c) == ["employment_type_not_stated"]


def test_a_stated_seniority_the_person_did_not_choose_breaks_the_constraint():
    c = Constraints(seniorities=("senior", "staff"))
    assert violations(job(seniority="intern"), c) == ["seniority"]
    assert violations(job(seniority="staff"), c) == []


def test_an_unknown_seniority_passes_and_is_reported_as_unverified():
    c = Constraints(seniorities=("senior",))
    assert violations(job(seniority="unknown"), c) == []
    assert unverified(job(seniority="unknown"), c) == ["seniority_not_stated"]


# ---- excluded companies ----------------------------------------


def test_an_excluded_company_is_matched_by_its_key_so_punctuation_and_legal_suffixes_do_not_hide_it():
    c = Constraints(excluded_company_keys=(company_key("Acme Inc"),))
    assert violations(job(company_name="ACME, Inc."), c) == ["excluded_company"]
    assert violations(job(company_name="Acme Corporation"), c) == ["excluded_company"]
    assert violations(job(company_name="Acmeron"), c) == []


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("ACME, Inc.", "acme"),
        ("Acme Incorporated", "acme"),
        ("Initech LLC", "initech"),
        ("Initech Demo Ltd", "initechdemo"),
        ("Globex Co.", "globex"),
        ("Globex Company", "globex"),
        ("Müller GmbH", "mller"),
        ("Soylent Private Limited", "soylent"),
        ("Hooli Pvt. Ltd.", "hooli"),
        ("Incredible Machines", "incrediblemachines"),  # a suffix is removed only as a whole word
        ("Costco", "costco"),
        ("Stark Industries 2", "starkindustries2"),
        ("", ""),
        (None, ""),
    ],
)
def test_company_key_is_lower_case_letters_and_digits_without_legal_suffixes(name, key):
    assert company_key(name) == key


# ---- what is never reported ----------------------------------------


def test_a_constraint_the_person_did_not_set_is_never_reported():
    everything_unusual = job(
        country="DE",
        city="Berlin",
        work_arrangements=("onsite",),
        employment_type="contract",
        seniority="intern",
        salary_max=1,
        salary_currency="EUR",
        visa_sponsorship="not_offered",
    )
    assert violations(everything_unusual, Constraints()) == []
    assert unverified(job(), Constraints()) == []


def test_every_constraint_at_once_is_reported_in_the_order_of_the_enum():
    c = Constraints(
        countries=("IN",),
        arrangements=("remote",),
        authorized_countries=("IN",),
        salary_floor=100,
        salary_currency="USD",
        employment_types=("full_time",),
        seniorities=("senior",),
        excluded_company_keys=(company_key("Initech Demo"),),
    )
    posting = job(
        country="DE",
        work_arrangements=("onsite",),
        employment_type="contract",
        seniority="intern",
        salary_max=1,
        salary_currency="USD",
    )
    assert violations(posting, c) == [
        "location",
        "remote_policy",
        "work_authorization",
        "salary_floor",
        "employment_type",
        "seniority",
        "excluded_company",
    ]


def test_every_gap_at_once_is_reported_in_the_order_of_the_enum():
    c = Constraints(
        countries=("IN",),
        arrangements=("remote",),
        salary_floor=100,
        salary_currency="USD",
        employment_types=("full_time",),
        seniorities=("senior",),
    )
    assert unverified(job(), c) == [
        "location_not_stated",
        "arrangement_not_stated",
        "salary_not_stated",
        "employment_type_not_stated",
        "seniority_not_stated",
    ]
