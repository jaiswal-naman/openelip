"""IDX-06: the location parser reads a place only when the text says it, and never guesses.

Where the cases come from: the plan's behaviour list; the generic examples in evals/location/GUIDELINE.md (they are
not rows of the sample); and the dev split of evals/location/sample.jsonl, cited by id in DEV_ROWS. No row of the test
split was opened. An expectation for a dev row is what the parser returns, which the label accepts ("New York City" is
the label "New York": the suite compares city names through GeoNames' alternate names).
"""

import pytest

from app.ingest.normalize import INDIA_CITIES, parse_location


def read(raw, hint=None):
    country, city, _ = parse_location(raw, hint)
    return country, city


# ---- what the parser must read ----------------------------------------

READ = [
    ("San Francisco, CA", None, ("US", "San Francisco")),
    ("Toronto, ON, Canada", None, ("CA", "Toronto")),
    ("London, UK", None, ("GB", "London")),
    ("Berlin", None, ("DE", "Berlin")),
    ("New York, NY", None, ("US", "New York City")),  # the GeoNames name of the place
    ("Bangalore, Karnataka, India", None, ("IN", "Bengaluru")),
    ("Gurgaon", None, ("IN", "Gurugram")),
    ("California", None, ("US", None)),
    ("Kyiv, Ukraine", None, ("UA", "Kyiv")),
    ("London", "United Kingdom", ("GB", "London")),
    ("Pune or Mumbai", None, ("IN", "Pune")),  # the first place listed wins
    ("London / Berlin", None, ("GB", "London")),
    ("New York, NY; San Francisco, CA", None, ("US", "New York City")),
    ("Austin, Texas, United States", None, ("US", "Austin")),
    ("Cambridge, MA", None, ("US", "Cambridge")),
    ("Munich (Hybrid)", None, ("DE", "Munich")),
    ("München, Deutschland", None, ("DE", "Munich")),
    ("Bangalore, KA", None, ("IN", "Bengaluru")),  # KA is Karnataka: not a country, not a state of the US
    ("NYC", None, ("US", "New York City")),
    ("SF", None, ("US", "San Francisco")),
    ("Singapore", None, ("SG", "Singapore")),
    ("Hong Kong", None, ("HK", "Hong Kong")),
    ("Atlanta, Georgia", None, ("US", "Atlanta")),
    ("Tbilisi, Georgia", None, ("GE", "Tbilisi")),
    ("Paris, TX", None, ("US", "Paris")),
    ("Remote - Brazil", None, ("BR", None)),
    ("Reykjavík", None, ("IS", "Reykjavík")),
    ("dublin", None, ("IE", "Dublin")),
]


@pytest.mark.parametrize("raw, hint, expected", READ)
def test_a_place_the_text_names_is_read(raw, hint, expected):
    assert read(raw, hint) == expected


def test_remote_and_the_country_are_separate_answers():
    assert parse_location("Remote - US") == ("US", None, True)
    assert parse_location("Remote") == (None, None, True)
    assert parse_location("Anywhere", country_hint="in", remote_hint=False) == ("IN", None, False)
    assert parse_location("Pune", remote_hint=True) == ("IN", "Pune", True)  # the board's flag wins over the text
    assert parse_location("Pune")[2] is None  # nothing says it either way


# ---- unknown stays unknown ----------------------------------------

UNKNOWN = [
    "HQ",
    "Multiple Locations",
    "EMEA",
    "Global",
    "Anywhere",
    "",
    None,
    "N/A",
    "TBD",
    "Various",
    "Remote",
    "Remote, Global",
    "Remote - EST",  # a time zone is not a place
    "Remote - Europe",
    "Update Location",
    "Open to relocation",
    "Full Time",  # time and part are the names of towns, and not what this says
    "Part-time",
    "SEA",  # Seattle's airport code and short for Southeast Asia: a reader would hesitate
    "CA",  # Canada or California
    "Remote - CA",
    "Remote - IN",  # India or Indiana
    "Georgia",  # the country or the state
    "Korea",
    "Kosovo",  # not an ISO 3166-1 country, so never reported as one
    "Pristina",
    "EMEA; London",  # a region is the first place listed and it carries no country
    "Europe, UK",
]


@pytest.mark.parametrize("raw", UNKNOWN)
def test_a_string_that_names_no_single_place_gives_no_country_and_no_city(raw):
    assert read(raw) == (None, None)


def test_a_string_that_is_not_a_place_at_all_is_unknown_however_long():
    assert read("lorem ipsum dolor sit amet " * 400) == (None, None)


@pytest.mark.parametrize("raw", ["Cambridge", "Birmingham", "Manchester", "Hamilton", "Valencia", "Richmond"])
def test_an_ambiguous_name_with_nothing_to_settle_it_gives_no_country_and_no_city(raw):
    # One place is not at least ten times larger than every other place of that name.
    assert read(raw) == (None, None)


def test_the_hint_settles_an_ambiguous_name_and_a_wrong_hint_settles_nothing():
    assert read("Cambridge", "US") == ("US", "Cambridge")
    assert read("Cambridge", "Canada") == ("CA", "Cambridge")
    assert read("Cambridge", "DE") == ("DE", None)  # the hint's country, but no city that fits it
    assert read("Hyderabad", "PK") == ("PK", "Hyderabad")


def test_a_country_cue_in_the_text_settles_an_ambiguous_name():
    assert read("Cambridge, UK") == ("GB", "Cambridge")
    assert read("Birmingham, Alabama") == ("US", "Birmingham")
    assert read("Hamilton, ON") == ("CA", "Hamilton")


def test_the_text_beats_the_hint_and_a_cue_beats_the_largest_city():
    assert read("Berlin", "US") == ("DE", "Berlin")  # the text names its own country
    assert read("London, Ontario") == ("CA", "London")
    assert read("Vancouver, WA") == ("US", "Vancouver")
    assert read("Paris, Italy") == ("IT", None)  # the text says Italy and no city of that name is there to read


def test_one_name_for_several_places_of_one_country_is_one_answer():
    assert read("Portland") == ("US", "Portland")  # Oregon and Maine
    assert read("Springfield") == ("US", "Springfield")


def test_two_cities_that_cannot_share_a_country_leave_the_string_unreadable():
    assert read("London, Paris") == (None, None)
    assert read("Berlin, Munich") == ("DE", "Berlin")


# ---- codes ----------------------------------------


def test_a_two_letter_code_after_a_city_is_the_state_when_the_city_is_there_and_a_country_when_not():
    assert read("Wilmington, DE") == ("US", "Wilmington")  # Delaware
    assert read("Berlin, DE") == ("DE", "Berlin")  # Germany
    assert read("Boulder, CO") == ("US", "Boulder")  # Colorado, not Colombia


def test_a_code_that_fits_no_city_is_something_else_and_is_ignored():
    assert read("Pune, IT") == ("IN", "Pune")  # IT is not Italy here
    assert read("Berlin, BLR") == ("DE", "Berlin")  # BLR is Belarus, and Bengaluru's airport
    assert read("Mumbai, IND") == ("IN", "Mumbai")  # but a three-letter code that fits the city is the country
    assert read("Perth, WA") == ("AU", "Perth")


def test_the_bare_codes_the_guideline_lists():
    assert read("TX") == ("US", None)
    assert read("CA", "US") == ("US", None)  # the hint settles it
    assert read("CA, USA") == ("US", None)  # so does the rest of the text
    assert read("Remote - CA", "CA") == ("CA", None)


@pytest.mark.parametrize("raw", ["U.S.", "Remote - U.S.A.", "Remote (US)", "US Remote", "Remote USA"])
def test_the_united_states_in_its_usual_spellings(raw):
    assert read(raw) == ("US", None)


def test_a_sentence_is_not_read_but_a_small_word_between_a_qualifier_and_a_place_is_skipped():
    assert read("Remote in Germany") == ("DE", None)
    assert read("Anywhere in the US") == ("US", None)
    assert read("Join us in Berlin") == (None, None)  # "join" is explained by nothing, so nothing here is read
    assert read("REMOTE IN GERMANY") == (None, None)  # "IN" in capitals is a code (India, Indiana), not the word


# ---- hints ----------------------------------------


@pytest.mark.parametrize(
    "hint, expected",
    [
        ("in", "IN"),
        ("IN", "IN"),
        ("India", "IN"),
        ("United States", "US"),
        ("USA", "US"),
        ("United States of America", "US"),
        ("UK", "GB"),
        ("United Kingdom", "GB"),
        ("IND", "IN"),
        ("XX", None),  # not a country: the old parser accepted any two letters
        ("Atlantis", None),
        ("", None),
    ],
)
def test_a_country_hint_is_read_as_a_name_or_a_code_and_checked(hint, expected):
    assert parse_location("Anywhere", country_hint=hint)[0] == expected


def test_a_hint_with_no_text_is_the_country_and_nothing_more():
    assert parse_location(None, country_hint="United Kingdom") == ("GB", None, None)
    assert parse_location("Hybrid", country_hint="DE") == ("DE", None, None)
    assert parse_location("Remote, EMEA", country_hint="DE")[:2] == ("DE", None)


# ---- the forms strings come in ----------------------------------------

FORMS = [
    ("US-CA-Menlo Park", ("US", "Menlo Park")),
    ("GB-London", ("GB", "London")),
    ("CH-Zurich-Observe", ("CH", "Zürich")),
    ("US-CA-Dublin", ("US", "Dublin")),  # California, not Ireland
    ("Dallas Warehouse", ("US", "Dallas")),
    ("Bengaluru-VTP, India", ("IN", "Bengaluru")),
    ("NYC-Privy", ("US", "New York City")),
    ("Downtown Chicago", ("US", "Chicago")),
    ("Central London", ("GB", "London")),
    ("Mexico - Update Location", ("MX", None)),
    ("Ukraine Anywhere", ("UA", None)),
    ("San Francisco Bay Area", ("US", None)),
    ("Bay Area", ("US", None)),
    ("Greater Boston Area", ("US", "Boston")),
    ("Remote; London", ("GB", "London")),  # how the work is done is not a place
    ("Washington, DC", ("US", "Washington")),
    ("Washington D.C.", ("US", "Washington")),
    ("Washington", ("US", None)),  # the state
    ("Winston-Salem, NC", ("US", "Winston-Salem")),
    ("Guinea-Bissau", ("GW", None)),
    ("Brooklyn, NY", ("US", "New York City")),
    ("Brooklyn, Ohio", ("US", "Brooklyn")),
    ("Manhattan", ("US", "New York City")),
    ("Manhattan, KS", ("US", "Manhattan")),
    ("Hybrid - San Francisco, New York City, Austin", ("US", "San Francisco")),
    ("Mexico", ("MX", None)),  # the country, not Mexico City
    ("Mexico City", ("MX", "Mexico City")),
    ("Ontario", ("CA", None)),  # the province, not the town in California
    ("Missouri", ("US", None)),
]


@pytest.mark.parametrize("raw, expected", FORMS)
def test_the_forms_postings_write_places_in(raw, expected):
    assert read(raw) == expected


# ---- what did not change ----------------------------------------


@pytest.mark.parametrize("name, canonical", sorted(INDIA_CITIES.items()))
def test_every_indian_city_the_foundation_knew_keeps_its_name(name, canonical):
    assert read(name) == ("IN", canonical)


def test_no_country_is_ever_a_default():
    for raw in ("zzzz", "Somewhere", "123", "Floor 3, Building B"):
        assert read(raw) == (None, None)


# ---- rows of the dev split, cited by id ----------------------------------------

DEV_ROWS = [
    ("001", "San Francisco, California, United States", None, "US", "San Francisco"),
    ("002", "New York City, NY; San Francisco, CA", None, "US", "New York City"),
    ("003", "New York, NY; Seattle, WA; San Francisco, CA; Chicago, IL; Atlanta, GA", None, "US", "New York City"),
    ("004", "Vancouver, British Columbia, Canada", None, "CA", "Vancouver"),
    ("005", "Remote - Washington D.C.", None, "US", "Washington"),
    ("006/059/087/090", "San Francisco", "United States", "US", "San Francisco"),
    ("007", "Mountain View, California", None, "US", "Mountain View"),
    ("008", "New York City", "United States", "US", "New York City"),
    ("009", "San Francisco, CA, US; Palo Alto, CA, US; Seattle, WA, US", None, "US", "San Francisco"),
    (
        "010",
        "Bellevue, Washington; Chicago, Illinois; San Francisco, California; Washington, DC",
        None,
        "US",
        "Bellevue",
    ),
    ("011", "Bellevue, WA; Chicago, IL; Menlo Park, CA; New York, NY", None, "US", "Bellevue"),
    ("012", "California, USA, Remote; Colorado, USA, Remote; Washington, USA, Remote", None, "US", None),
    ("013/014/031/076/100", "New York, NY (HQ)", "USA", "US", "New York City"),
    ("015", "Bengaluru, Karnataka, India", None, "IN", "Bengaluru"),
    ("016", "Tokyo", None, "JP", "Tokyo"),
    (
        "017",
        "Atlanta; Austin; Baltimore; Boston; Chicago; Conshohocken; Dallas; Houston; Minneapolis; "
        "New York City; Pittsburgh; Washington DC",
        None,
        "US",
        "Atlanta",
    ),
    ("018", "IN-Bangalore-MSO", "India", "IN", "Bengaluru"),
    ("019", "N/A", None, None, None),
    ("020", "SF, SEA, NY, Remote-US", None, "US", "San Francisco"),
    ("021", "San Francisco, CA; Palo Alto, CA; Seattle, WA; New York, NY; Remote, US", None, "US", "San Francisco"),
    ("022", "Ciudad de México", "Mexico", "MX", "Mexico City"),
    ("023", "New York, New York, USA; San Francisco, California, USA", None, "US", "New York City"),
    ("024/085", "Mountain View, California; San Francisco, California", None, "US", "Mountain View"),
    ("025/060", "Noida, Uttar Pradesh", "IN", "IN", "Noida"),
    ("026", "Mexico City", None, "MX", "Mexico City"),
    ("027", "Seattle or San Francisco or Chicago or New York City or US-Remote", None, "US", "Seattle"),
    ("028/054", "Bengaluru, India", None, "IN", "Bengaluru"),
    ("029", "Tokyo, Japan", None, "JP", "Tokyo"),
    ("030", "Nashville, TN", None, "US", "Nashville"),
    ("032/074/099", "Toronto, Canada", None, "CA", "Toronto"),
    ("033", "Palo Alto", None, "US", "Palo Alto"),
    ("034", "Singapore, SG", None, "SG", "Singapore"),
    ("035", "Canada - Remote (ON, AB, BC, or NS Only)", None, "CA", None),
    ("036", "US-NY-New York", "United States", "US", "New York City"),
    ("037", "Sydney", None, "AU", "Sydney"),
    ("038/039", "Miami", "United States", "US", "Miami"),
    ("040/043", "Singapore", None, "SG", "Singapore"),
    ("041/077/084", "Singapore, Singapore", None, "SG", "Singapore"),
    ("042", "dublin", None, "IE", "Dublin"),
    ("044", "Salt Lake City, Utah, United States", None, "US", "Salt Lake City"),
    ("045", "San Francisco Bay Area or Remote (U.S.)", None, "US", None),
    ("046/057", "Osaka, Japan", None, "JP", "Osaka"),
    ("047", "Portugal", None, "PT", None),
    ("048", "Madrid", None, "ES", "Madrid"),
    ("049", "Gurugram", None, "IN", "Gurugram"),
    ("050", "Remote - California", None, "US", None),
    ("051/069", "San Francisco, CA, US; Remote, US", None, "US", "San Francisco"),
    ("052", "Berlin, Germany", None, "DE", "Berlin"),
    ("053", "Milan, Italy", None, "IT", "Milan"),
    ("055", "Remote, Global", None, None, None),
    ("056", "San Francisco, CA | New York City, NY", None, "US", "San Francisco"),
    ("058/080", "Bengaluru", None, "IN", "Bengaluru"),
    ("061", "London, United Kingdom", None, "GB", "London"),
    ("062", "Chicago, IL; Denver, CO; Westlake, TX", None, "US", "Chicago"),
    ("063", "São Paulo", "Brazil", "BR", "São Paulo"),
    ("064", "Bucharest", "Romania", "RO", "Bucharest"),
    ("065", "Menlo Park, CA", None, "US", "Menlo Park"),
    ("066/072", "Mumbai, Maharashtra", "IN", "IN", "Mumbai"),
    ("067", "London, UK", "United Kingdom", "GB", "London"),
    ("068", "Hong Kong", None, "HK", "Hong Kong"),
    ("070", "Sonipat, Haryana", "IN", "IN", "Sonīpat"),  # GeoNames' spelling; the label "Sonipat" is an alternate name
    ("071", "Hybrid - Sydney", None, "AU", "Sydney"),
    ("073/094", "Remote - USA", None, "US", None),
    ("075", "Mumbai, IND", None, "IN", "Mumbai"),
    ("078", "San Francisco, California", None, "US", "San Francisco"),
    ("079/086", "San Francisco, CA | New York City, NY | Seattle, WA", None, "US", "San Francisco"),
    ("081", "New York, NY", None, "US", "New York City"),
    ("082", "Gurugram, Haryana", "IN", "IN", "Gurugram"),
    ("083", "SEA", None, None, None),
    ("088", "Santa Clara, California, USA", None, "US", "Santa Clara"),
    ("089", "Denver, Colorado, USA", None, "US", "Denver"),
    ("091", "United States", None, "US", None),
    ("092", "Remote - United States", None, "US", None),
    ("093", "San Francisco", "United States", "US", "San Francisco"),
    ("095", "Dallas Warehouse", None, "US", "Dallas"),
    ("096", "New Jersey", None, "US", None),
    ("097", "Dublin, Ireland", None, "IE", "Dublin"),
    ("098", "Amherst, MA", "US", "US", "Amherst"),
]


@pytest.mark.parametrize("ids, raw, hint, country, city", DEV_ROWS, ids=[row[0] for row in DEV_ROWS])
def test_the_dev_rows_used_while_building_the_parser(ids, raw, hint, country, city):
    assert read(raw, hint) == (country, city), f"dev rows loc-{ids}"
