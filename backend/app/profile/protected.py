"""Protected-characteristic screen (REC-07, AI_SYSTEM section 4.9).

Two rule-based screens (no model: the same words always give the same answer). Both return the category of a
protected characteristic the text states, or None, and never the matched text, so a caller has nothing to store or log.
Raw resume text is not touched (owner decision G4).

screen(text) is the narrow one: label-style lines ("Age: 27", "Religion: Jainism", "Date of birth: ...") and a few bare
words. A resume line is screened with this one (plan 03-03), because a resume holds many ordinary sentences and a wrong
drop costs a real fact. Bare words such as "age" or "gender" are not matched, since they are far more often a job-title
or skill word ("Chief Age Officer tooling", "gender pay gap dashboard"). Work authorization ("Authorized to work in
India") is a hard-constraint kind and is deliberately not screened.

screen_typed(text) is the wide one, for text a person typed into the record (plan 03-04): a fact, a constraint, a
preference, a reason, a company or a city, in the app or through chat. It is the narrow screen plus plain phrasings:
"I am 45 years old", "Born in 1979", "I am a practising Muslim", "Married with two kids", "I use a wheelchair", "Black
woman engineer". Each plain pattern needs a person in the sentence (I am, I have, my, as a) or a statement that stands
alone ("Hindu, vegetarian", "Female, 32"), so ordinary career sentences pass: "managed a team of 40", "a 20 year old
codebase", "Led the Catholic Charities website rebuild", "Single sign-on".

ponytail: a pattern screen is the v1 ceiling, and it is not complete. It misses a characteristic worded another way
("my faith guides me", "the girls at home"), in another language, or spelled out ("forty-five years old"), and it
refuses a few ordinary sentences that use these words about the writer ("I am a man of few words"). A model check can
be added in front of these tables later, never in place of them. Citizenship and visa statements are not screened (they
are the work_authorization constraint), and neither is a bare nationality ("I am Indian").
"""

import re

# "Age: 27", "Age - 27", "Age = 27". A hyphen needs a space on each side: "gender-neutral" and "race-based" are words.
_LABEL = r"(?:\s*[:=\N{EN DASH}\N{EM DASH}]|\s+-\s+)"

# Ordered: the first row that matches names the category. Case-insensitive.
_NARROW: tuple[tuple[str, str], ...] = (
    ("age", rf"\bdate\s+of\s+birth\b|\bbirth\s*date\b|\bd\.?o\.?b\.?(?:{_LABEL}|\s+\d)|\bage{_LABEL}\s*\d"),
    ("marital_status", r"\bmarital\s+status\b"),
    ("religion", rf"\breligion{_LABEL}"),
    ("gender", rf"\b(?:gender|sex){_LABEL}\s*\w"),
    ("sexual_orientation", r"\bsexual\s+orientation\b"),
    ("nationality", rf"\bnationality{_LABEL}"),
    ("ethnicity", rf"\b(?:race|ethnicity|ethnic\s+origin){_LABEL}"),
    ("caste", r"\bcaste\b"),
    ("disability", r"\bdisabilit(?:y|ies)\b"),
    ("pregnancy", r"\bpregnan(?:t|cy)\b"),
)

# Pieces of the plain phrasings.
_I_AM = r"\bI(?:\s+am|'m)\s+(?:an?\s+)?"  # "I am", "I'm a", "I am an"
_START = r"(?:^|[.;:!?\n-]\s*)"  # the start of a statement: "Married with two kids" is one, "a married couple" is not
_STOPS = r"(?:[,.;!?]|$)"  # the statement ends here
_WORD_END = r"(?![\w-])"  # a whole word, not the front of a compound ("single-handedly", "man-hours")
_THEN = rf"(?=\s*{_STOPS}|\s+(?:and|but|so|with|who|from|since|to)\b)"  # it stops, or a joining word follows
_RELIGIONS = (
    r"(?:muslims?|moslem|hindus?|christians?|catholics?|protestants?|sikhs?|jews?|jewish|buddhists?|jains?|parsis?"
    r"|zoroastrian|atheists?|agnostics?|mormons?|baha'?is?|evangelical|pentecostal|methodists?|baptists?|anglicans?"
    r"|lutherans?|presbyterians?|sunni|shia|shiite|ahmadi|taoists?|rastafarian|pagans?|wiccans?|quakers?|adventists?)"
)
_FAITHS = (
    r"(?:islam|hinduism|christianity|judaism|buddhism|sikhism|jainism|catholicism|zoroastrianism|mormonism|paganism)"
)
# "Catholic Charities" and "Methodist Hospital" are organisations, and "agnostic about frameworks" is about tools.
_NOT_A_NAME = (
    r"(?!\s+(?:charit(?:y|ies)|church(?:es)?|aid|relief|universit(?:y|ies)|college|school|hospital|health|diocese"
    r"|services|cent(?:er|re)|foundation|organi[sz]ation|community|mission|temple|mosque|society|association"
    r"|institute|council|federation|trust|fund|group|network|youth|coalition|alliance|bank|press|books|studies"
    r"|about|to|towards|regarding|when|on)\b)"
)
_DEVOUT = r"(?:(?:practi[cs]ing|devout|observant|non-practi[cs]ing|born[- ]again|orthodox|strict|proud)\s+)?"
_A_RELIGION = rf"{_DEVOUT}{_RELIGIONS}{_WORD_END}{_NOT_A_NAME}"
_ETHNICITIES = (
    r"(?:black|asian|hispanic|latino|latina|latinx|caucasian|white|african|african[- ]american|arab|indigenous"
    r"|native\s+american|south\s+asian|east\s+asian|pacific\s+islander|romani)"
)
_PERSON = r"(?:woman|man|women|men|female|male|person|lady|girl|boy|guy)"
_GENDERS = (
    r"(?:woman|man|female|male|girl|boy|lady|guy|transgender|trans|non-?binary|genderqueer|gender-?fluid|agender"
    r"|intersex|cisgender)(?!\s+scouts?\b)"
)
_ORIENTATIONS = r"(?:gay|lesbian|bisexual|bi|pansexual|asexual|queer|homosexual|heterosexual|lgbtq?\+?|lgbtqia\+?)"
_CASTES = r"(?:brahmin|dalit|kshatriya|vaishya|shudra)"
_DISABLED = (
    r"(?:disabled|deaf|blind|autistic|neurodivergent|dyslexic|dyspraxic|paraly[sz]ed|visually\s+impaired"
    r"|hearing\s+impaired|hard\s+of\s+hearing|wheelchair[- ]user|amputee)"
)
_CONDITIONS = (
    r"(?:cancer|diabetes|depression|anxiety|ptsd|ocd|bipolar|schizophrenia|epilepsy|asthma|hiv|aids|hepatitis"
    r"|multiple\s+sclerosis|lupus|crohn'?s|arthritis|migraines?|insomnia|heart\s+(?:disease|condition)"
    r"|kidney\s+(?:disease|failure)|(?:chronic|medical|health|heart|mental\s+health)\s+(?:condition|illness|problem)"
    r"|illness|disease|disorder)(?![\w-])(?!\s+(?:research|software|detection|screening|diagnostics?|registry|drug))"
)
_PARENT = r"(?:mother|father|mom|mum|mama|dad|parent)"
# A child, not a child process or a child component, and not baby steps.
_CHILD = (
    r"(?:kids?|children|child(?!\s*(?:process|node|class|component|thread|element|table|task|job|record|row))"
    r"|sons?|daughters?|babies|baby(?!\s+steps)|toddlers?|newborns?|infants?|twins)"
)
_COUNT = r"(?:an?|\d+|two|three|four|five|six|seven|several)"
_MONTH = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
_EXPECTING = (
    r"expecting\s+(?:an?\s+(?:baby|child|boy|girl)|twins|(?:my|our)\s+(?:first|second|third|baby|child)"
    rf"|in\s+(?:{_MONTH}|spring|summer|autumn|fall|winter))"
)
_LEAVE = r"\bI(?:\s+am|'m)\s+(?:on|taking|going\s+on)\s+"

# The plain phrasings, tried after the narrow table. Ordered; the first row that matches names the category.
# Case-insensitive.
_PLAIN: tuple[tuple[str, str], ...] = (
    # age: "I am 45 years old", "I'm 32, looking for ...", "Born in 1979", "my age", "in my forties"
    ("age", rf"{_I_AM}\d{{2}}(?:[- ]?(?:years?|yrs?)[- ]?(?:old|of\s+age)|\s?(?:y/o|yo)){_WORD_END}"),
    ("age", rf"\bI(?:\s+am|'m)\s+\d{{2}}\s*(?:{_STOPS}|and\b)"),
    ("age", rf"{_START}aged?\s+\d{{2}}\b"),
    ("age", rf"{_START}\d{{2}}[- ]?(?:years?|yrs?)[- ]?old\s*{_STOPS}"),
    ("age", r"\bI\s+was\s+born\b[^.;!?\n]{0,25}?\b(?:19[3-9]\d|20[01]\d)\b"),
    ("age", rf"{_START}born\b[^.;!?\n]{{0,25}}?\b(?:19[3-9]\d|20[01]\d)\b"),
    ("age", r"\bmy\s+age\b"),
    (
        "age",
        r"\bin\s+my\s+(?:(?:early|mid|late)[- ]?)?(?:twenties|thirties|forties|fifties|sixties|seventies|[2-7]0'?s)\b",
    ),
    # marital status: "Married with two kids", "I am married", "my wife", "I am single, so ..."
    ("marital_status", rf"{_START}(?:married|divorced|widowed|unmarried|newly\s+married){_WORD_END}{_THEN}"),
    (
        "marital_status",
        rf"\bI(?:\s+am|'m)\s+(?:\w+ly\s+|newly\s+|recently\s+|not\s+)?(?:married|divorced|widowed|unmarried){_WORD_END}",
    ),
    ("marital_status", rf"\bI(?:\s+am|'m)\s+(?:currently\s+|still\s+)?single(?=\s*{_STOPS}|\s+(?:and|but|so)\b)"),
    ("marital_status", r"\b(?:newly|recently|just\s+got|getting|got)\s+married\b"),
    ("marital_status", rf"{_I_AM}(?:widow|widower){_WORD_END}"),
    (
        "marital_status",
        r"\bmy\s+(?:ex-)?(?:wife|husband|spouse|fianc[e\N{LATIN SMALL LETTER E WITH ACUTE}]e?|girlfriend|boyfriend"
        r"|better\s+half)\b|\bmy\s+divorce\b|\bgoing\s+through\s+a\s+divorce\b",
    ),
    # family status: children, parenthood and caring for family
    (
        "family_status",
        rf"\bI\s+(?:have|'ve\s+got|have\s+got)\s+{_COUNT}\s+(?:(?:young|small|little|new|teenage)\s+)?{_CHILD}{_WORD_END}",
    ),
    ("family_status", rf"\bmy\s+(?:[\w-]+\s+)?{_CHILD}{_WORD_END}"),
    ("family_status", rf"{_I_AM}(?:[\w-]+\s+)?{_PARENT}{_WORD_END}(?=\s*{_STOPS}|\s+(?:and|but|so|of|to|with|who)\b)"),
    ("family_status", rf"\b{_PARENT}\s+of\s+{_COUNT}\b|\bsingle\s+{_PARENT}\b"),
    ("family_status", rf"\b(?:we|my\s+\w+)\s+(?:are|is|'re)\s+{_EXPECTING}|\bwe're\s+{_EXPECTING}"),
    ("family_status", rf"{_LEAVE}(?:paternity|parental)\s+leave\b"),
    (
        "family_status",
        r"\bcaring\s+for\s+(?:my|a)\s+(?:[\w-]+\s+)?(?:mother|father|parents?|child(?:ren)?|kids?|son|daughter"
        r"|husband|wife|spouse)\b",
    ),
    ("family_status", rf"{_I_AM}(?:primary\s+)?caregiver{_WORD_END}"),
    # religion: "I am a practising Muslim", "as a Hindu ...", "Hindu, vegetarian", "my faith", "I fast during Ramadan"
    ("religion", rf"{_I_AM}{_A_RELIGION}"),
    ("religion", rf"\bas\s+an?\s+{_A_RELIGION}"),
    ("religion", rf"{_START}{_RELIGIONS}\s*(?:[,;.]|$)"),
    ("religion", rf"\b{_RELIGIONS}\s+(?:{_PERSON}|mother|father)\b"),
    ("religion", rf"\bI\s+was\s+(?:raised|brought\s+up)\s+(?:as\s+(?:an?\s+)?)?{_RELIGIONS}{_WORD_END}"),
    (
        "religion",
        r"\bmy\s+(?:faith|religion|religious\s+\w+|church|mosque|temple|synagogue|gurdwara|imam|pastor|priest|rabbi)\b",
    ),
    ("religion", rf"\bI\s+(?:follow|practi[cs]e|believe\s+in|converted\s+to)\s+(?:the\s+)?{_FAITHS}\b"),
    (
        "religion",
        r"\bI\s+(?:observe|keep|celebrate)\s+(?:the\s+)?(?:sabbath|shabbat|ramadan|lent|kosher|halal|passover"
        r"|yom\s+kippur)\b",
    ),
    (
        "religion",
        r"\bI\s+pray\s+(?:five|5|daily|regularly|every|several|at\s+work|during|on)\b"
        r"|\bI\s+fast\s+(?:during|for|on)\b",
    ),
    ("religion", r"\bI\s+attend\s+(?:church|mosque|temple|synagogue|gurdwara|mass)\b"),
    ("religion", r"\bI\s+wear\s+(?:a\s+)?(?:hijab|niqab|burqa|turban|yarmulke|kippah|headscarf|head\s+scarf)\b"),
    ("religion", r"\bprayer\s+(?:breaks?|times?)\b|\bfriday\s+prayers?\b"),
    # gender: "I am a woman", "As a woman in tech ...", "Female, 32", "my pronouns"
    ("gender", rf"{_I_AM}(?:(?:trans|cis|young|older|working|proud)\s+)?{_GENDERS}{_WORD_END}"),
    (
        "gender",
        rf"\bas\s+an?\s+(?:(?:young|older|working|single|trans|disabled)\s+)*(?:woman|man|female|male){_WORD_END}",
    ),
    ("gender", r"\bI\s+(?:self-)?identify\s+as\b|\bmy\s+(?:gender|pronouns?)\b"),
    ("gender", rf"{_START}(?:female|male|woman|man|girl|boy|lady|gentleman)\s*(?:[,;.]|$)"),
    # sexual orientation
    ("sexual_orientation", rf"{_I_AM}(?:(?:openly|proudly|out)\s+)?{_ORIENTATIONS}{_WORD_END}"),
    (
        "sexual_orientation",
        rf"\bI(?:\s+am|'m)\s+(?:a\s+)?straight(?=\s*{_STOPS}|\s+(?:and|man|woman|guy|male|female)\b)",
    ),
    ("sexual_orientation", r"\bmy\s+sexuality\b|\bsame[- ]sex\s+(?:partner|couple|marriage|spouse)\b"),
    # nationality and national origin (citizenship is the work_authorization constraint and is not screened)
    ("nationality", r"\bmy\s+nationality\b|\bnational\s+origin\b"),
    ("nationality", rf"{_I_AM}(?:immigrant|refugee|asylum\s+seeker){_WORD_END}"),
    # ethnicity: "I am Black", "Black woman engineer", "of Indian descent", "my ethnicity"
    (
        "ethnicity",
        rf"{_I_AM}(?:(?:proud|first-generation|second-generation|mixed-race|mixed)\s+)?{_ETHNICITIES}{_WORD_END}"
        rf"(?:{_THEN}|\s+(?:{_PERSON}|engineer|developer|professional|candidate|mother|father|student|graduate))",
    ),
    ("ethnicity", rf"\b{_ETHNICITIES}\s+{_PERSON}\b"),
    ("ethnicity", rf"{_START}{_ETHNICITIES}\s*(?:[,;.]|$)"),
    ("ethnicity", r"\bof\s+[\w-]+(?:\s+[\w-]+)?\s+(?:descent|origin|heritage|ancestry)\b"),
    (
        "ethnicity",
        r"\bmy\s+(?:ethnicity|ethnic\s+(?:origin|background)|race|racial\s+background|heritage|skin\s+colou?r|ancestry)\b",
    ),
    # caste
    ("caste", rf"{_I_AM}{_CASTES}{_WORD_END}"),
    # disability and health: "I use a wheelchair", "I have diabetes", "I am on medication"
    ("disability", rf"{_I_AM}{_DISABLED}{_WORD_END}"),
    (
        "disability",
        r"\bI\s+(?:use|need|rely\s+on)\s+(?:a\s+)?(?:wheelchair|walking\s+(?:stick|frame|aid)|crutches|guide\s+dog"
        r"|hearing\s+aids?|prosthe\w+)\b|\bI(?:\s+am|'m)\s+in\s+a\s+wheelchair\b",
    ),
    (
        "disability",
        r"\bI\s+have\s+(?:adhd|autism|dyslexia|dyspraxia|tourette'?s?|cerebral\s+palsy|down\s+syndrome|hearing\s+loss"
        r"|vision\s+loss)\b",
    ),
    (
        "health",
        r"\bI\s+(?:have|'ve\s+got|have\s+got|was\s+diagnosed\s+with|suffer\s+from|am\s+living\s+with|live\s+with)\s+"
        rf"(?:(?:a|an)\s+)?(?:(?:chronic|severe|mild|type\s+[12])\s+)?{_CONDITIONS}",
    ),
    (
        "health",
        rf"{_I_AM}(?:in\s+remission|a\s+cancer\s+survivor|chronically\s+ill|immunocompromised|diabetic|depressed"
        rf"|hiv[- ]positive|an?\s+(?:addict|alcoholic)|in\s+recovery){_WORD_END}",
    ),
    (
        "health",
        r"\bI\s+(?:am|'m|take)\s+(?:on\s+)?(?:daily\s+)?(?:medication|insulin|chemotherapy|dialysis|antidepressants?)\b",
    ),
    ("health", r"\bI(?:\s+am|'m)\s+recovering\s+from\b"),
    (
        "health",
        r"\bmy\s+(?:illness|diagnosis|medication|chemo(?:therapy)?|therapy\s+sessions|surgery|medical\s+condition"
        r"|health\s+condition|chronic\s+condition)\b",
    ),
    # pregnancy
    ("pregnancy", rf"\bI(?:\s+am|'m)\s+(?:\w+\s+)?{_EXPECTING}"),
    ("pregnancy", rf"\bmy\s+(?:due\s+date|maternity\s+leave)\b|{_LEAVE}maternity\s+leave\b"),
)
_NARROW_COMPILED = tuple((category, re.compile(pattern, re.IGNORECASE)) for category, pattern in _NARROW)
_PLAIN_COMPILED = tuple((category, re.compile(pattern, re.IGNORECASE)) for category, pattern in _PLAIN)

CATEGORIES = tuple(dict.fromkeys(category for category, _ in _NARROW + _PLAIN))


def screen(text: str) -> str | None:
    """The category of a protected characteristic stated as a label or a bare word, or None: for resume lines."""
    for category, pattern in _NARROW_COMPILED:
        if pattern.search(text):
            return category
    return None


def screen_typed(text: str) -> str | None:
    """The category of a protected characteristic the text states, in a label or in a plain sentence, or None: for
    text a person typed into the record."""
    if (category := screen(text)) is not None:
        return category
    plain = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")  # a phone keyboard types the curly one
    for category, pattern in _PLAIN_COMPILED:
        if pattern.search(plain):
            return category
    return None
