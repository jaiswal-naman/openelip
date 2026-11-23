"""Layer 1 of the tailoring verifier (AI_SYSTEM 4.7, TLR-02, invariant 3): code checks every proposed edit against the
facts that support it, before anything stores or shows it. No model call and no database access.

The reasons, in the order they are checked (an edit gets the first that applies; INDEX shared contracts):

    no_supporting_fact   an alias that names no fact, no stated support, an edit that touches what code writes
                         (headings, degrees) or an add_from_fact whose text is not the fact word for word, or a
                         reorder that drops an item
    changed_employer     names the employer of a role the supporting facts do not belong to
    changed_title        names another role's title, or a seniority word (senior, lead, director ...) no fact uses
    changed_date         a year, or a month of a year, that no supporting fact states
    changed_number       a number that no supporting fact states (number words count: "two" is 2)
    new_entity           a tool, organisation, acronym or capitalised name no supporting fact or confirmed skill holds,
                         a term of the job's requirement texts no supporting fact holds, or a reorder that adds an item

The heuristics are plain and conservative: they discard when unsure. They do not read meaning. An edit that passes
every check can still claim too much ("led" for "contributed to"): that is layer 2, verify.entail (pipeline.py).
"""

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from . import resume
from .resume import Fact, ResumeDoc
from .schemas import EditItem

REASONS = ("no_supporting_fact", "changed_employer", "changed_title", "changed_date", "changed_number", "new_entity")

REWORDABLE = frozenset({"achievement", "project"})  # free text. A skill or certification is a name; headings are code's
REMOVABLE = frozenset({"achievement", "project", "skill", "certification"})  # one bullet or skill (never a heading)
ADDABLE = frozenset({"skill", "certification"})  # what a chat-stated fact can be (record.propose allow-list)
MAX_GRAM = 3  # requirement terms are one to three words

NUMBER_WORDS = {
    **{
        word: value
        for value, word in enumerate(
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
            "seventeen eighteen nineteen twenty".split(),
            1,
        )
    },
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "dozen": 12,
}
# Ordinals, fractions and multiplier words state a quantity as much as a digit does ("first", "halved", "2x"), so the
# number check sees them (05 fix A4). By value, in the same space as the digits: "first" is 1, "half" is 0.5.
# "second" is left out (it is also a unit of time).
QUANTITY_WORDS = {
    **{
        word: str(value)
        for value, word in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1)
        if word != "second"
    },
    "half": "0.5",
    "halve": "0.5",
    "halved": "0.5",
    "halves": "0.5",
    "½": "0.5",
    "quarter": "0.25",
    "quarters": "0.25",
    "¼": "0.25",
    "¾": "0.75",
    "⅓": "0.33",
    "⅔": "0.67",
    "double": "2",
    "doubled": "2",
    "twice": "2",
    "triple": "3",
    "tripled": "3",
    "thrice": "3",
    "quadruple": "4",
    "quadrupled": "4",
}
_MULTIPLIER = re.compile(r"(\d+(?:\.\d+)?)x")  # 2x, 10x, 1.5x
MONTHS = {
    name: number
    for number, names in enumerate(
        ("jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august")
        + ("sep sept september", "oct october", "nov november", "dec december"),
        1,
    )
    for name in names.split()
}
# assumption: words that say seniority or management. A word here that no supporting fact uses is a changed title.
RANK_WORDS = frozenset(
    "senior sr junior jr lead principal staff head chief director manager vp executive founder".split()
)
# assumption: function words, and the words every posting and every resume use. They are not job terms and a capitalised
# one in the middle of a sentence is not an entity. "one" is also a number word; the number check still sees it.
STOP_WORDS = frozenset(
    (
        "a about above across after all also an and any are as at be been being but by can could do does for from had "
        "has have how i if in into is it its may more most must new no not of on one or other our out over per plus "
        "such than that the their them then these they this those to under up us was we were what when where which "
        "while who will with within without would you your "
        "experience experienced ability able strong good excellent proven knowledge understanding skills skill work "
        "working worked years year team teams including include includes etc preferred required requirement "
        "requirements bonus responsibilities using use used well"
    ).split()
)
ABBREVIATIONS = frozenset({"e.g", "i.e"})  # a full stop after these does not end a sentence

_TOKEN = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![^\W_])"  # 2,000  1,250.50
    r"|\d+(?:\.\d+)?(?![^\W_])"  # 40  3.5  (not 3D, 10x or 2M: a digit in a word is part of the word)
    r"|[^\W_]+(?:[+#]+|(?:[./][^\W_]+)+)?"  # a word; C++ and C#; Node.js, CI/CD, A/B
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d\d")
_ISO = re.compile(r"(\d{4})(?:-(\d{2}))?")
_SENTENCE_END = re.compile(r"[.!?\n]")
_ATTRIBUTE_NAMES = ("employer", "title", "institution", "degree", "name")


@dataclass(frozen=True)
class Requirement:
    """One thing the job asks for: its id in the job index, its text and whether it is required or preferred."""

    id: int
    text: str
    importance: str


@dataclass(frozen=True)
class Scan:
    """What a piece of text states, without reading its meaning."""

    tokens: tuple[str, ...]  # lower-cased words and numbers, in order
    dates: tuple[tuple[int, int | None], ...]  # (year, month); "March 2021" is (2021, 3), "2021" is (2021, None)
    numbers: frozenset[str]  # by value: "2,000", "2000" and "two thousand" aside, "two" and "2" are the same
    shapes: tuple[str, ...]  # lower-cased tokens that look like a name or a tool, once each


def _fold(token: str) -> str:
    """A token for comparing: lower case, and a plural is its singular (the same on both sides, so only consistency
    matters)."""
    low = token.casefold()
    return low[:-1] if len(low) > 3 and low.endswith("s") and not low.endswith("ss") else low


def _value(number: str) -> str:
    return format(Decimal(number.replace(",", "")).normalize(), "f")


def _starts_sentence(text: str, found: Sequence["re.Match[str]"], index: int) -> bool:
    if index == 0:
        return True
    gap = text[found[index - 1].end() : found[index].start()]
    return _SENTENCE_END.search(gap) is not None and found[index - 1].group().casefold() not in ABBREVIATIONS


def _is_shape(word: str, *, initial: bool) -> bool:
    """Does this word look like a name, a tool or an acronym? A capitalised word is one unless it starts a sentence
    (every sentence starts with one) or is a function word."""
    if len(word) < 2 or _NUMBER.fullmatch(word) or word.casefold() in ABBREVIATIONS:
        return False
    if any(char.isdigit() for char in word) or any(char in word for char in "+#./"):
        return True
    if word.isupper() or any(char.isupper() for char in word[1:]):  # AWS; JavaScript, iOS
        return True
    return word[0].isupper() and not initial and word.casefold() not in STOP_WORDS


def scan(text: str) -> Scan:
    found = list(_TOKEN.finditer(text))
    words = [match.group() for match in found]
    lows = [word.casefold() for word in words]
    dates: list[tuple[int, int | None]] = []
    numbers: set[str] = set()
    month_tokens: set[int] = set()
    for index, word in enumerate(words):
        if _YEAR.fullmatch(word):  # a year is both a date and a number: it is a date
            month = MONTHS.get(lows[index - 1]) if index else None
            if month is not None:
                month_tokens.add(index - 1)  # "March" in "March 2021" dates the year; it is not a name
            dates.append((int(word), month))
        elif _NUMBER.fullmatch(word):
            numbers.add(_value(word))
        elif lows[index] in NUMBER_WORDS:
            numbers.add(str(NUMBER_WORDS[lows[index]]))
        elif lows[index] in QUANTITY_WORDS:
            numbers.add(QUANTITY_WORDS[lows[index]])
        elif (multiplier := _MULTIPLIER.fullmatch(lows[index])) is not None:
            numbers.add(_value(multiplier.group(1)))
    shapes = [
        lows[index]
        for index, word in enumerate(words)
        if index not in month_tokens and _is_shape(word, initial=_starts_sentence(text, found, index))
    ]
    return Scan(tuple(lows), tuple(dates), frozenset(numbers), tuple(dict.fromkeys(shapes)))


def _grams(tokens: Sequence[str]) -> Iterator[tuple[str, ...]]:
    """Every run of one to three words, folded. A run never takes in a number."""
    folded = [None if _NUMBER.fullmatch(token) else _fold(token) for token in tokens]
    for start in range(len(folded)):
        for size in range(1, MAX_GRAM + 1):
            gram = folded[start : start + size]
            if len(gram) < size or None in gram:
                break
            yield tuple(word for word in gram if word is not None)


_STOP_FOLDED = frozenset(_fold(word) for word in STOP_WORDS)
_RANK_FOLDED = frozenset(_fold(word) for word in RANK_WORDS)


def job_phrases(texts: Iterable[str]) -> frozenset[tuple[str, ...]]:
    """The terms of requirement texts (or single terms): runs of one to three words that neither start nor end with a
    stop word."""
    found: set[tuple[str, ...]] = set()
    for text in texts:
        for gram in _grams(scan(text).tokens):
            if gram[0] not in _STOP_FOLDED and gram[-1] not in _STOP_FOLDED and len(gram[0]) > 1:
                found.add(gram)
    return frozenset(found)


# ---- what facts state ----------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """The words, numbers and dates a set of facts uses: what a new text may reuse."""

    text: str  # lower-cased, one line: employer and title mentions are looked for here
    tokens: frozenset[str]  # folded
    phrases: frozenset[tuple[str, ...]]  # folded runs of one to three words
    years: frozenset[int]
    months: frozenset[tuple[int, int]]
    numbers: frozenset[str]


def _words_of(fact: Fact) -> list[str]:
    values = [fact.attributes.get(name) for name in _ATTRIBUTE_NAMES]
    return [fact.statement, *(value for value in values if isinstance(value, str))]


def vocabulary(facts: Sequence[Fact], headers: Sequence[str] = ()) -> Vocabulary:
    """What these facts state. `headers` are extra names (the employer and title of a role a bullet sits under): they
    add words, never dates or numbers."""
    years: set[int] = set()
    months: set[tuple[int, int]] = set()
    numbers: set[str] = set()
    parts = [*headers]
    scans = [scan(header) for header in headers]
    for fact in facts:
        for part in _words_of(fact):
            parts.append(part)
            seen = scan(part)
            scans.append(seen)
            numbers |= seen.numbers
            for year, month in seen.dates:
                years.add(year)
                if month is not None:
                    months.add((year, month))
        for key in ("start_date", "end_date"):
            iso = _ISO.fullmatch(str(fact.attributes.get(key)))
            if iso:
                years.add(int(iso.group(1)))
                if iso.group(2):
                    months.add((int(iso.group(1)), int(iso.group(2))))
    return Vocabulary(
        text=" ".join(" ".join(parts).split()).casefold(),
        tokens=frozenset(_fold(token) for seen in scans for token in seen.tokens),
        phrases=frozenset(gram for seen in scans for gram in _grams(seen.tokens)),
        years=frozenset(years),
        months=frozenset(months),
        numbers=frozenset(numbers),
    )


@dataclass(frozen=True)
class Known:
    """What the whole record and the job say, as opposed to the facts that support one edit."""

    employers: tuple[str, ...]  # of every role, lower-cased
    titles: tuple[str, ...]
    skill_tokens: frozenset[str]  # folded words of every confirmed skill
    skill_phrases: frozenset[tuple[str, ...]]
    job: frozenset[tuple[str, ...]]  # the terms of the requirement texts
    names: frozenset[
        str
    ]  # folded name-like tokens (a capitalised word or acronym) of every fact and job text, any case


def _known(roles: Iterable[Fact], skills: Iterable[str], job_texts: Iterable[str], facts: Iterable[Fact] = ()) -> Known:
    def names(key: str) -> tuple[str, ...]:
        values = (role.attributes.get(key) for role in roles)
        return tuple(dict.fromkeys(" ".join(v.split()).casefold() for v in values if isinstance(v, str) and v.strip()))

    skill_scans = [scan(skill) for skill in skills]
    job_list = list(job_texts)
    name_tokens = {
        _fold(shape)
        for text in [*job_list, *(part for fact in facts for part in _words_of(fact))]
        for shape in scan(text).shapes
    }
    return Known(
        employers=names("employer"),
        titles=names("title"),
        skill_tokens=frozenset(_fold(token) for seen in skill_scans for token in seen.tokens),
        skill_phrases=frozenset(gram for seen in skill_scans for gram in _grams(seen.tokens)),
        job=job_phrases(job_list),
        names=frozenset(name_tokens),
    )


def _mentions(haystack: str, name: str) -> bool:
    return re.search(rf"(?<![^\W_]){re.escape(name)}(?![^\W_])", haystack) is not None


def _text_reason(text: str, support: Sequence[Fact], headers: Sequence[str], known: Known) -> str | None:
    """The first of the five text reasons that applies to this new text, given what its supporting facts state."""
    vocab = vocabulary(support, headers)
    seen = scan(text)
    written = " ".join(text.split()).casefold()
    for name in known.employers:
        if _mentions(written, name) and not _mentions(vocab.text, name):
            return "changed_employer"
    for name in known.titles:
        if _mentions(written, name) and not _mentions(vocab.text, name):
            return "changed_title"
    if any(_fold(token) in _RANK_FOLDED and _fold(token) not in vocab.tokens for token in seen.tokens):
        return "changed_title"
    for year, month in seen.dates:
        if year not in vocab.years or (month is not None and (year, month) not in vocab.months):
            return "changed_date"
    if seen.numbers - vocab.numbers:
        return "changed_number"
    allowed = vocab.tokens | known.skill_tokens
    if any(_fold(shape) not in allowed for shape in seen.shapes):
        return "new_entity"
    # the same names in any case: "kubernetes" is as much a name as "Kubernetes" when the record or the job writes it so
    if any(_fold(token) in known.names and _fold(token) not in allowed for token in seen.tokens):
        return "new_entity"
    supported = vocab.phrases | known.skill_phrases
    if any(gram in known.job and gram not in supported for gram in _grams(seen.tokens)):
        return "new_entity"
    return None


# ---- the checks on one edit ----------------------------------------


@dataclass(frozen=True)
class Context:
    """Everything layer 1 needs about one candidate and one job. facts are numbered f1..fN in the order given."""

    facts: Mapping[str, Fact]  # alias -> fact
    aliases: Mapping[str, str]  # fact id -> alias
    by_id: Mapping[str, Fact]
    base: ResumeDoc
    shown: frozenset[str]  # ids of the facts the base resume shows
    groups: tuple[tuple[str, ...], ...]  # the aliases of each group a reorder may permute, in resume order
    known: Known


def build_context(facts: Sequence[Fact], requirements: Sequence[Requirement]) -> Context:
    aliases = {fact.id: f"f{number}" for number, fact in enumerate(facts, 1)}
    base = resume.build_base(facts, name="", email="")
    return Context(
        facts={aliases[fact.id]: fact for fact in facts},
        aliases=aliases,
        by_id={fact.id: fact for fact in facts},
        base=base,
        shown=frozenset(resume.fact_ids(base)),
        groups=tuple(tuple(aliases[bullet.fact_id] for bullet in group) for group in resume.reorder_groups(base)),
        known=_known(
            [fact for fact in facts if fact.kind == "role"],
            [resume.item_text(fact) for fact in facts if fact.kind == "skill"],
            [requirement.text for requirement in requirements],
            facts,
        ),
    )


def _headers(facts: Sequence[Fact], ctx: Context) -> list[str]:
    """The employer and title of the role each fact sits under: a bullet may name its own employer and title."""
    headers: list[str] = []
    for fact in facts:
        parent = ctx.by_id.get(fact.parent_fact_id) if fact.parent_fact_id else None
        if parent is not None and parent.kind == "role":
            headers += [value for key in ("employer", "title") if isinstance(value := parent.attributes.get(key), str)]
    return headers


def _rephrase(edit: EditItem, ctx: Context) -> str | None:
    named = [ctx.facts.get(alias) for alias in (edit.target or "", *edit.supporting)]
    target, support = named[0], [fact for fact in named if fact is not None]
    if not edit.supporting or target is None or len(support) != len(named):
        return "no_supporting_fact"
    if target.kind not in REWORDABLE or target.id not in ctx.shown:
        return "no_supporting_fact"
    support = list(dict.fromkeys(support))
    return _text_reason(edit.after_text, support, _headers(support, ctx), ctx.known)


def _remove(edit: EditItem, ctx: Context) -> str | None:
    target = ctx.facts.get(edit.target or "")
    if target is None or any(alias not in ctx.facts for alias in edit.supporting):
        return "no_supporting_fact"
    # a removal adds no words, and the fact removed is the one that supports it
    return None if target.kind in REMOVABLE and target.id in ctx.shown else "no_supporting_fact"


def _add(edit: EditItem, ctx: Context) -> str | None:
    fact = ctx.facts.get(edit.supporting[0])
    if fact is None or fact.id in ctx.shown or fact.kind not in ADDABLE:
        return "no_supporting_fact"  # no such fact, or the resume shows it already, or it is not a thing to add
    return None if resume.line_text(edit.after_text) == resume.item_text(fact) else "no_supporting_fact"


def reorder_group(edit: EditItem, ctx: Context) -> tuple[str, ...] | None:
    """The group a reorder works on (its first item says which), in resume order, or None when the first item is not a
    bullet or a skill the resume shows."""
    return next((group for group in ctx.groups if edit.order[0] in group), None)


def _reorder(edit: EditItem, ctx: Context) -> str | None:
    group = reorder_group(edit, ctx)
    if group is None:
        return "no_supporting_fact"  # not the bullets of one role, nor the skills line
    given = set(edit.order)
    if set(group) - given:
        return "no_supporting_fact"  # an item dropped
    return "new_entity" if given - set(group) else None  # an item slipped in


def check(edit: EditItem, ctx: Context) -> str | None:
    """The reason layer 1 discards this edit, or None when it passes."""
    if edit.op == "reorder":
        return _reorder(edit, ctx)
    if edit.op == "remove":
        return _remove(edit, ctx)
    if edit.op == "add_from_fact":
        return _add(edit, ctx)
    return _rephrase(edit, ctx)


def unsupported_reason(
    text: str,
    supporting: Sequence[Fact],
    *,
    skills: Iterable[str] = (),
    job_terms: Iterable[str] = (),
    roles: Sequence[Fact] = (),
) -> str | None:
    """The same rules for free text, such as a packet sentence: the reason this text says more than its supporting
    facts, or None. `skills` are the candidate's confirmed skill names, `job_terms` the requirement texts (or single
    terms) of the job. Pass `roles`, every role fact of the candidate, so a sentence naming another role's employer or
    title gets that reason; without it an unfamiliar organisation is still caught, as new_entity. A sentence that names
    its employer should cite the role fact too."""
    if not supporting:
        return "no_supporting_fact"
    return _text_reason(text, supporting, (), _known(roles, skills, job_terms))
