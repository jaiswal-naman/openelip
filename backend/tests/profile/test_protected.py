"""REC-07: a text that states a protected characteristic is detected, in a label and in a plain sentence; work
authorization and ordinary career sentences are not.

screen() is the narrow screen (labels and a few bare words) that resume lines go through. screen_typed() is the wide one
for text a person typed: it adds plain first-person and descriptive phrasings. Both sets below, the sentences that must
be caught and the ordinary ones that must pass, are written out so a new pattern that blocks a normal resume line, or a
pattern that stops catching a phrasing, fails here.
"""

import json
from pathlib import Path

import pytest

from app.profile.protected import CATEGORIES, screen, screen_typed

GOLD = Path(__file__).resolve().parents[3] / "evals" / "resume_extraction" / "facts" / "gold.jsonl"

# ---- labels and bare words: caught by both screens -------------------------------------------------------------

LABEL_STATES = [
    ("Date of birth: 04/03/1998", "age"),
    ("DOB: 04/03/1998", "age"),
    ("Age: 27", "age"),
    ("Age - 27", "age"),
    ("Age \N{EN DASH} 27", "age"),
    ("Marital status: single", "marital_status"),
    ("Religion: Jainism", "religion"),
    ("Gender: female", "gender"),
    ("Sex - F", "gender"),
    ("Sexual orientation: prefer not to say", "sexual_orientation"),
    ("Nationality: Indian", "nationality"),
    ("Race: example", "ethnicity"),
    ("Caste category: OBC", "caste"),
    ("I have a disability", "disability"),
    ("I am pregnant", "pregnancy"),
]


@pytest.mark.parametrize(("text", "category"), LABEL_STATES)
def test_a_label_or_a_bare_word_returns_its_category_from_both_screens(text, category):
    assert screen(text) == category
    assert screen_typed(text) == category


# ---- plain sentences: caught by the wide screen only -----------------------------------------------------------

# What a person types: first person ("I am", "I have", "my"), a statement that stands alone, or a description.
PLAIN_STATES = [
    # age
    ("I am 45 years old", "age"),
    ("I'm 45 years old", "age"),
    ("I am a 52 year old engineer", "age"),
    ("I'm a 38-year-old developer", "age"),
    ("I am 45", "age"),
    ("I'm 32, looking for a remote role", "age"),
    ("I am 29 and want to relocate", "age"),
    ("Born in 1979", "age"),
    ("I was born in 1985", "age"),
    ("I was born on 4 March 1979", "age"),
    ("Born 04/03/1979", "age"),
    ("I'm in my late forties", "age"),
    ("I am in my 50s", "age"),
    ("Aged 45", "age"),
    ("Age 45", "age"),
    ("My age is a concern for recruiters", "age"),
    ("45 years old", "age"),
    ("I'm 45yo", "age"),
    ("I am 38 y/o", "age"),
    # marital status
    ("Married with two kids", "marital_status"),
    ("I am married", "marital_status"),
    ("I'm happily married", "marital_status"),
    ("I am divorced", "marital_status"),
    ("I'm a widow", "marital_status"),
    ("I am single, so I can relocate", "marital_status"),
    ("Newly married", "marital_status"),
    ("I just got married", "marital_status"),
    ("My wife is relocating for work", "marital_status"),
    ("my husband works in Pune", "marital_status"),
    ("I live with my spouse", "marital_status"),
    ("Divorced, 45", "marital_status"),
    # family status
    ("I have two kids", "family_status"),
    ("I have 3 children", "family_status"),
    ("I have a young daughter", "family_status"),
    ("My kids start school in June", "family_status"),
    ("I need to pick up my children at 4pm", "family_status"),
    ("I am a single mother", "family_status"),
    ("I'm a father of two", "family_status"),
    ("I'm a stay-at-home dad", "family_status"),
    ("Mother of three", "family_status"),
    ("We are expecting a baby", "family_status"),
    ("We're expecting twins", "family_status"),
    ("I am caring for my elderly mother", "family_status"),
    # religion
    ("I am a practising Muslim", "religion"),
    ("I'm Hindu", "religion"),
    ("I am a Christian", "religion"),
    ("I'm a devout Catholic", "religion"),
    ("As a Jewish person I need Shabbat off", "religion"),
    ("as a Muslim I pray five times a day", "religion"),
    ("Hindu, vegetarian", "religion"),
    ("My faith matters to me", "religion"),
    ("I observe the Sabbath", "religion"),
    ("I fast during Ramadan", "religion"),
    ("I pray five times a day", "religion"),
    ("I attend church every Sunday", "religion"),
    ("I practice Buddhism", "religion"),
    ("I was raised Catholic", "religion"),
    ("Muslim woman", "religion"),
    ("I wear a hijab", "religion"),
    ("I'm an atheist", "religion"),
    # gender
    ("I am a woman", "gender"),
    ("I'm female", "gender"),
    ("I am a man", "gender"),
    ("I'm a trans woman", "gender"),
    ("I am non-binary", "gender"),
    ("As a woman in tech I want a supportive team", "gender"),
    ("Female, 32", "gender"),
    ("Male", "gender"),
    ("My pronouns are she/her", "gender"),
    ("I identify as a woman", "gender"),
    # sexual orientation
    ("I am gay", "sexual_orientation"),
    ("I'm a lesbian", "sexual_orientation"),
    ("I am bisexual", "sexual_orientation"),
    ("I'm queer", "sexual_orientation"),
    ("my sexuality", "sexual_orientation"),
    ("We are a same-sex couple", "sexual_orientation"),
    # nationality (national origin, not citizenship)
    ("my nationality", "nationality"),
    ("I am an immigrant", "nationality"),
    ("I'm a refugee", "nationality"),
    # ethnicity
    ("I am Black", "ethnicity"),
    ("I'm Asian", "ethnicity"),
    ("I am a Latina engineer", "ethnicity"),
    ("Black woman engineer", "ethnicity"),
    ("I'm of Indian descent", "ethnicity"),
    ("my ethnicity", "ethnicity"),
    ("African American woman", "ethnicity"),
    ("Asian, 32", "ethnicity"),
    # caste
    ("I am a Brahmin", "caste"),
    ("I'm Dalit", "caste"),
    # disability
    ("I use a wheelchair", "disability"),
    ("I'm in a wheelchair", "disability"),
    ("I am deaf", "disability"),
    ("I'm autistic", "disability"),
    ("I have ADHD", "disability"),
    ("I have dyslexia", "disability"),
    ("I'm visually impaired", "disability"),
    ("I am hard of hearing", "disability"),
    # health
    ("I have diabetes", "health"),
    ("I have depression", "health"),
    ("I was diagnosed with cancer", "health"),
    ("I'm in remission", "health"),
    ("I have a chronic health condition", "health"),
    ("I am on medication for anxiety", "health"),
    ("I take insulin every day", "health"),
    ("I suffer from migraines", "health"),
    ("my medical condition", "health"),
    ("I am HIV positive", "health"),
    ("I'm recovering from surgery", "health"),
    # pregnancy
    ("I'm expecting a baby", "pregnancy"),
    ("I'm expecting in March", "pregnancy"),
    ("I'm on maternity leave", "pregnancy"),
]


@pytest.mark.parametrize(("text", "category"), PLAIN_STATES)
def test_a_plain_sentence_returns_its_category_from_the_wide_screen(text, category):
    assert screen_typed(text) == category


def test_every_category_has_a_plain_example_and_every_example_names_a_known_category():
    assert {category for _, category in PLAIN_STATES} == set(CATEGORIES)


# The ten phrasings the verification of Phase 3 tried. The narrow screen let every one of them through, which is why
# text a person typed goes through the wide one.
VERIFICATION_PHRASES = [
    "I am 45 years old",
    "Born in 1979",
    "I am a woman",
    "Married with two kids",
    "I am a practising Muslim",
    "I am gay",
    "I use a wheelchair",
    "Black woman engineer",
    "Hindu, vegetarian",
    "Female, 32",
]


@pytest.mark.parametrize("text", VERIFICATION_PHRASES)
def test_the_phrasings_the_narrow_screen_missed_are_caught_by_the_wide_one(text):
    assert screen(text) is None  # resume lines keep the narrow screen: see test_a_resume_line_keeps_the_narrow_screen
    assert screen_typed(text) is not None


def test_a_curly_apostrophe_from_a_phone_keyboard_is_the_same_as_a_straight_one():
    assert screen_typed("I\N{RIGHT SINGLE QUOTATION MARK}m 45 years old") == "age"
    assert screen_typed("I\N{RIGHT SINGLE QUOTATION MARK}m a trans woman") == "gender"


@pytest.mark.parametrize("text", ["I AM A WOMAN", "married with two KIDS", "i am a practising muslim"])
def test_the_case_of_the_words_does_not_matter(text):
    assert screen_typed(text) is not None


def test_the_result_is_a_category_name_never_the_matched_text():
    for text in ("Date of birth: 04/03/1998", "I was born on 4 March 1979", "I am a practising Muslim"):
        result = screen_typed(text)
        assert result in CATEGORIES
        assert not any(word in result for word in ("1998", "1979", "Muslim", "March"))


# ---- ordinary text: passes both screens ----------------------------------------------------------------------------

# Career sentences, employer names and numbers that contain the words above, or the same words used as a name, a
# product or a number. None of them states a characteristic of the writer.
ORDINARY = [
    # work authorization is a hard constraint, not a protected characteristic
    "Authorized to work in India",
    "Work authorization: US citizen",
    "I am a US citizen",
    "I need visa sponsorship",
    # words that are job-title or skill words
    "Chief Age Officer tooling",
    "age verification service",
    "Race condition debugging in the queue worker",
    "Gender pay gap dashboard for HR analytics",
    "Nationality is not used as a filter in this system",
    "Worked on gender-neutral language tooling",
    "Nationality-based tax rules engine",
    "Race-neutral hiring analytics",
    "Religion-based holiday calendar service",
    "Age-appropriate content filtering service",
    "Aged inventory reporting dashboard",
    "Python, Go and SQL",
    "Remote only",
    "No heavy travel",
    "Fintech",
    "Kubernetes",
    # numbers that are not an age
    "Managed a team of 40",
    "managed a team of 40 engineers across three time zones",
    "Team of 45 engineers",
    "Modernized a 20 year old payments platform",
    "Maintained a 15-year-old Perl codebase",
    "The codebase is 20 years old.",
    "40 years of combined team experience",
    "Reduced latency by 45 percent",
    "I have 10 years of experience in backend engineering",
    "I am 5 years into my career",
    "I'm 100% remote",
    "I am 24/7 on call",
    "I'm 10 minutes from the office",
    "Co-founded a startup in 2015 with 40 employees",
    "Joined a startup born in 2015",
    "A company born in 1995 and still growing",
    "Born in the cloud architecture",
    "Users aged 18 to 24 convert best",
    # employers and products named after a faith or a person
    "Led the Catholic Charities website rebuild",
    "As a Catholic Charities volunteer I built the intake form",
    "Built a Christian Aid donations portal",
    "Methodist Hospital",
    "Baptist Health",
    "Jewish Federation of Greater Chicago",
    "Hindu Kush trekking app",
    "Christian Dior ecommerce migration",
    "Hindustan Unilever",
    "Jain Irrigation Systems",
    "Worked with the Sikh Coalition on accessibility audits",
    "Ran the Ramadan marketing campaign",
    "Diwali sale pricing engine",
    "Volunteer coordinator at Catholic Relief Services",
    "I am agnostic about frameworks",
    # compound words and phrases that start like a status
    "Built tax software for married filing jointly returns",
    "Single sign-on integration with Okta",
    "Designed a single page application",
    "I am a single point of contact for the account",
    "I am single-handedly responsible for the build pipeline",
    "Divorced the billing service from the monolith",
    "Took my first baby steps in Rust",
    "my child process terminated cleanly",
    "I have a child component that re-renders",
    "Rebuilt the Children's Hospital patient portal",
    "Built a children's hospital scheduling system",
    "I am a parent company analyst",
    "I am a girl scout troop leader",
    "I am a woman-owned business supplier",
    "Female-founded startup",
    "Male/female split analysis",
    "Woman in tech mentorship program lead",
    "Women in Tech mentoring programme",
    "Built the pronoun field (she/her, he/him, they/them) in the profile editor",
    "Built the Black Friday checkout",
    "Asian markets expansion lead",
    "Grew the African developer community to 5,000 members",
    "I am a Black Duck engineer",
    "I am a white-hat security tester",
    "Built wheelchair accessibility checks for a transit app",
    "I use a screen reader to test accessibility",
    "I have cancer research experience",
    "I am engaged in open source work",
    # plain first-person sentences that are about work
    "I use Python and Go daily",
    "I use a standing desk",
    "I have strong opinions about code review",
    "I have a degree in computer science",
    "I have two years of experience with Go",
    "I am a manager",
    "I am a manager of 12 engineers",
    "I am a Staff Engineer",
    "I am a mentor to junior engineers",
    "I am passionate about accessibility",
    "I am looking for a staff role",
    "I'm based in Pune",
    "I'm expecting a callback from two recruiters",
    "I am expecting offers from two companies",
    "We are expecting a decision next week",
    "I pray this deploy works",
    "I fast-track releases",
    "my team of 12",
    "my manager",
    "my business partner",
    "my partner at the consultancy",
    "My manager asked me to lead the migration",
    "Senior Backend Engineer at Globex Example Ltd",
    "Mentored three junior engineers through their first production launches",
    "Prefer small teams with a clear mission",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_an_ordinary_career_sentence_is_not_flagged_by_either_screen(text):
    assert screen(text) is None
    assert screen_typed(text) is None


def test_the_empty_text_and_plain_whitespace_are_not_flagged():
    assert screen_typed("") is None and screen_typed("   \n ") is None


def _gold_lines():
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        for number, text in enumerate(item["lines"], 1):
            yield item["key"], number, text, number in item["protected_lines"]


def test_no_line_of_the_120_invented_resumes_is_flagged_except_the_six_that_state_a_characteristic():
    """About 3,400 lines of resume sentences are a fair sample of what a normal career record is written in: the wide
    screen must leave every one alone, and must still find the six lines that are not facts."""
    flagged, wrongly_passed, seen = [], [], 0
    for key, number, text, is_protected in _gold_lines():
        seen += 1
        for screen_used in (screen, screen_typed):
            found = screen_used(text)
            if is_protected and found is None:
                wrongly_passed.append((key, number, screen_used.__name__))
            elif found is not None and not is_protected:
                flagged.append((key, number, found))
    assert seen > 3000
    assert not flagged, f"lines of an ordinary resume were flagged: {flagged[:5]}"
    assert not wrongly_passed, f"a line that states a characteristic was let through: {wrongly_passed}"
