"""Pure functions that turn raw posting fields into typed values.

Rule: never guess. If a value cannot be recognised it stays None, and the
matching layer treats None as "the posting does not say".
"""

import functools
import hashlib
import html
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

import geonamescache  # type: ignore[import-untyped]

# A bare city name is read as one place only when it is this many times larger than every other place of that name.
DOMINANT_POPULATION_RATIO = 10

# City -> canonical name. Only cities whose name is unambiguous for India.
INDIA_CITIES = {
    "bengaluru": "Bengaluru",
    "bangalore": "Bengaluru",
    "gurugram": "Gurugram",
    "gurgaon": "Gurugram",
    "mumbai": "Mumbai",
    "bombay": "Mumbai",
    "pune": "Pune",
    "chennai": "Chennai",
    "kolkata": "Kolkata",
    "noida": "Noida",
    "new delhi": "New Delhi",
    "delhi": "New Delhi",
    "ahmedabad": "Ahmedabad",
    "jaipur": "Jaipur",
    "kochi": "Kochi",
    "indore": "Indore",
    "chandigarh": "Chandigarh",
    "coimbatore": "Coimbatore",
    "thiruvananthapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram",
    "hyderabad": "Hyderabad",
    "navi mumbai": "Navi Mumbai",
    "thane": "Thane",
    "mysuru": "Mysuru",
    "mysore": "Mysuru",
    "vadodara": "Vadodara",
    "nagpur": "Nagpur",
    "bhubaneswar": "Bhubaneswar",
}
# Country names and short forms, besides the names GeoNames itself uses. "U.S." is read as the code US (see _token).
COUNTRY_NAMES = {
    "india": "IN",
    "united states": "US",
    "usa": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "canada": "CA",
    "germany": "DE",
    "singapore": "SG",
    "australia": "AU",
    "ireland": "IE",
    "netherlands": "NL",
    "france": "FR",
    "japan": "JP",
    "brazil": "BR",
    "poland": "PL",
    "spain": "ES",
    "mexico": "MX",
    "israel": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "indonesia": "ID",
    "united states of america": "US",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "deutschland": "DE",
    "holland": "NL",
    "czech republic": "CZ",
    "turkiye": "TR",
}
# Places that name no one country (GUIDELINE rule 7). The first one in a string ends the search: "EMEA; London" is unknown.
REGIONS = frozenset(
    "emea apac latam dach mena anz eu eea nordics benelux baltics scandinavia europe asia africa oceania americas "
    "global worldwide international na est edt cst cdt mst mdt pst pdt cet gmt utc ist bst".split()
    + ["the americas", "north america", "south america", "latin america", "central america", "middle east"]
    + ["asia pacific", "european union"]
)
# Regions inside one country: the country is known, the city is not.
AREAS = {"bay area": "US", "san francisco bay area": "US", "sf bay area": "US", "silicon valley": "US"}
# Words that say how or where the work is done, or that are not places at all, though some are also names of towns.
QUALIFIERS = frozenset(
    "remote hybrid onsite flexible anywhere wfh virtual distributed nationwide statewide relocation travel office "
    "offices hq headquarters warehouse campus site plant factory store branch field home metro metropolitan area "
    "greater downtown midtown central north south east west center centre hub annex plaza based only update "
    "location locations multiple various other open any all tbd tba tbc none unknown full part time contract "
    "permanent temporary ncr rural urban region district state national city nan null undefined bay valley sar".split()
)
# Small words between a qualifier and a place ("Remote in Germany"). Lower case only: "IN" is a code.
CONNECTIVES = frozenset({"in", "the", "of", "within", "near", "across"})
# Short forms of cities, as (GeoNames name, country).
SHORT_FORMS = {"sf": ("San Francisco", "US"), "nyc": ("New York City", "US")}
# The five boroughs are New York City (GUIDELINE rule 3); GeoNames lists each as a city of its own.
NYC_BOROUGHS = frozenset({"The Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"})
# Canada's provinces and territories: code -> (name, the admin1 code GeoNames gives its cities).
CANADA = {
    "AB": ("Alberta", "01"),
    "BC": ("British Columbia", "02"),
    "MB": ("Manitoba", "03"),
    "NB": ("New Brunswick", "04"),
    "NL": ("Newfoundland and Labrador", "05"),
    "NS": ("Nova Scotia", "07"),
    "ON": ("Ontario", "08"),
    "PE": ("Prince Edward Island", "09"),
    "QC": ("Quebec", "10"),
    "SK": ("Saskatchewan", "11"),
    "YT": ("Yukon", "12"),
    "NT": ("Northwest Territories", "13"),
    "NU": ("Nunavut", "14"),
}
# India's states and territories that are not also cities. Without them "Goa" would be the town in the Philippines.
# Punjab is left out: it is a state in India and a province in Pakistan.
INDIA_STATES = frozenset(
    "andhra pradesh|arunachal pradesh|assam|bihar|chhattisgarh|goa|gujarat|haryana|himachal pradesh|jharkhand|"
    "karnataka|kerala|madhya pradesh|maharashtra|manipur|meghalaya|mizoram|nagaland|odisha|orissa|rajasthan|sikkim|"
    "tamil nadu|telangana|tripura|uttar pradesh|uttarakhand|west bengal|jammu and kashmir|ladakh".split("|")
)
# Codes in GeoNames' country table that are not (or no longer) ISO 3166-1 alpha-2 codes. Never reported as a country.
NOT_ISO = frozenset({"XK", "CS", "AN"})
MAX_WORDS = 4  # the longest place name scanned for: "san francisco bay area"

_LISTS = re.compile(r"[;/|\n]| or | and ", re.I)  # separate places; the first place listed wins
_JOINS = re.compile(r"[,()]")  # join the parts of one place: "San Francisco, CA, United States"
_DASHES = re.compile(r"[-–—]")
_INITIALS = re.compile(r"(?:[A-Za-z]\.){2,3}")  # U.S. D.C.
_LATIN = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")
_LETTERS = str.maketrans(
    {
        "ø": "o",
        "đ": "d",
        "ł": "l",
        "æ": "ae",
        "œ": "oe",
        "ı": "i",
        "ð": "d",
        "þ": "th",
        "'": "",
        "’": "",
        ".": "",
        "-": " ",
        ",": " ",
    }
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9+#.]+")


def html_to_text(raw: str | None) -> str:
    """Strip tags and entities. Greenhouse double-escapes its HTML, hence two unescapes."""
    if not raw:
        return ""
    # ponytail: regex tag stripping; swap for an HTML parser if layout-aware text is ever needed.
    text = html.unescape(html.unescape(raw))
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h[1-6]|/div)\s*/?>", "\n", text)
    text = _TAG.sub(" ", text)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _fold(text: str) -> str:
    """Lower case, no accents, dots or apostrophes, hyphens and commas as spaces: "Köln" is "koln", "St. John's" is "st johns"."""
    plain = unicodedata.normalize("NFKD", text.casefold().translate(_LETTERS))
    return " ".join("".join(ch for ch in plain if not unicodedata.combining(ch)).split())


class _Place(NamedTuple):
    name: str
    country: str
    admin1: str
    population: int


@dataclass(frozen=True)
class _Gazetteer:
    places: tuple[_Place, ...]
    by_name: dict[str, tuple[int, ...]]  # folded main name -> indexes into places
    by_alias: dict[str, tuple[int, ...]]  # folded alternate name of four letters or more -> indexes into places
    countries: dict[str, str]  # folded name -> ISO 3166-1 alpha-2
    iso2: frozenset[str]
    iso3: dict[str, str]
    subdivisions: dict[str, tuple[str, str | None]]  # folded state or province name -> (country, admin1 or None)
    codes: dict[str, tuple[tuple[str, str], ...]]  # upper-case state or province code -> ((country, admin1), ...)


@functools.cache
def _gazetteer() -> _Gazetteer:
    """GeoNames cities of 15,000 people or more (geonamescache), read on the first call and kept."""
    cache = geonamescache.GeonamesCache()
    places: list[_Place] = []
    by_name: dict[str, list[int]] = defaultdict(list)
    by_alias: dict[str, list[int]] = defaultdict(list)
    for city in cache.get_cities().values():
        index = len(places)
        places.append(_Place(city["name"], city["countrycode"], city["admin1code"], city["population"]))
        main = _fold(city["name"])
        if len(main) >= 3 and _LATIN.fullmatch(main):
            by_name[main].append(index)
        seen = {main}
        for alias in city["alternatenames"]:
            if not alias or ord(alias[0]) > 0x24F:  # a script other than Latin
                continue
            key = _fold(alias)
            # Four letters or more: shorter alternates are airport codes ("SEA") and common words.
            if len(key) >= 4 and key not in seen and _LATIN.fullmatch(key):
                seen.add(key)
                by_alias[key].append(index)
    country_rows = {iso: row for iso, row in cache.get_countries().items() if iso not in NOT_ISO}
    countries = {_fold(row["name"]): iso for iso, row in country_rows.items()}
    countries.update({_fold(name): iso for name, iso in COUNTRY_NAMES.items()})
    us_states = cache.get_us_states()
    subdivisions: dict[str, tuple[str, str | None]] = {
        _fold(row["name"]): ("US", code) for code, row in us_states.items()
    }
    subdivisions.update({_fold(name): ("CA", admin1) for name, admin1 in CANADA.values()})
    subdivisions.update({_fold(name): ("IN", None) for name in INDIA_STATES})
    codes: dict[str, tuple[tuple[str, str], ...]] = {code: (("US", code),) for code in us_states}
    codes.update({code: (("CA", admin1),) for code, (_, admin1) in CANADA.items()})
    return _Gazetteer(
        tuple(places),
        {key: tuple(found) for key, found in by_name.items()},
        {key: tuple(found) for key, found in by_alias.items()},
        countries,
        frozenset(country_rows),
        {row["iso3"]: iso for iso, row in country_rows.items()},
        subdivisions,
        codes,
    )


class _Reading(NamedTuple):
    """One thing a piece of text can mean. kind is region, area, country, alpha3, sub (a state or province) or city."""

    kind: str
    country: str | None = None
    admin1: str | None = None
    places: tuple[int, ...] = ()


class _Token(NamedTuple):
    readings: tuple[_Reading, ...]
    key: str  # the folded text; INDIA_CITIES is keyed by it
    code: bool  # an upper-case code such as "TX" or "IND": it counts only when it stands alone or agrees with the city


def _readings(key: str, g: _Gazetteer) -> list[_Reading]:
    if key in REGIONS:
        return [_Reading("region")]
    if key in AREAS:
        return [_Reading("area", AREAS[key])]
    if key in SHORT_FORMS:
        name, short_country = SHORT_FORMS[key]
        return [
            _Reading("city", places=tuple(i for i in g.by_name[_fold(name)] if g.places[i].country == short_country))
        ]
    found: list[_Reading] = []
    country = g.countries.get(key)
    if country:
        found.append(_Reading("country", country))
    if key in g.subdivisions:
        found.append(_Reading("sub", *g.subdivisions[key]))
    places = g.by_name.get(key) or g.by_alias.get(key) or ()
    if country:  # a country's name is the country, and a city only where the country's own city is named the same
        places = tuple(i for i in places if g.places[i].country == country and _fold(g.places[i].name) == key)
    elif key in g.subdivisions and key != "new york":
        places = ()  # "Ontario" is the province, not the town in California
    if places:
        found.append(_Reading("city", places=places))
    return found


def _code_readings(word: str, g: _Gazetteer) -> list[_Reading]:
    if len(word) == 2:
        found = [_Reading("country", word)] if word in g.iso2 else []
        return found + [_Reading("sub", country, admin1) for country, admin1 in g.codes.get(word, ())]
    return [_Reading("alpha3", g.iso3[word])] if word in g.iso3 else []


def _token(words: list[str], g: _Gazetteer, code_ok: bool) -> _Token | None:
    key = _fold(" ".join(words))
    if len(words) == 1 and key in QUALIFIERS:
        return None
    if found := _readings(key, g):
        return _Token(tuple(found), key, False)
    if len(words) == 1:
        word = words[0].replace(".", "").upper() if _INITIALS.fullmatch(words[0]) else words[0]
        if word == "US":  # a country anywhere in capitals ("US & Canada"), never the pronoun "us"
            return _Token((_Reading("country", "US"),), key, False)
        if code_ok and word.isalpha() and word.isupper() and (codes := _code_readings(word, g)):
            return _Token(tuple(codes), key, True)
    return None


def _skippable(word: str) -> bool:
    return word in CONNECTIVES or _fold(word) in QUALIFIERS or not any(ch.isalpha() for ch in word)


def _scan(words: list[str], g: _Gazetteer) -> list[_Token]:
    """Greedy, longest name first, so "Mexico City" is one place and "Dallas Warehouse" is Dallas. Every word must be a
    name or a qualifier: a part with a word nothing explains is not read at all (a sentence is not a place)."""
    lone = sum(not _skippable(word) for word in words) == 1
    found: list[_Token] = []
    i = 0
    while i < len(words):
        after_city = bool(found) and any(r.kind == "city" for r in found[-1].readings)
        for size in range(min(MAX_WORDS, len(words) - i), 0, -1):
            # An upper-case code ("TX") counts when it is the whole part ("Remote - TX") or follows a city ("Austin TX").
            if token := _token(words[i : i + size], g, lone or after_city):
                found.append(token)
                i += size
                break
        else:
            if not _skippable(words[i]):
                return []
            i += 1
    return found


def _is_name(text: str, g: _Gazetteer) -> bool:
    key = _fold(text)
    return key in g.by_name or key in g.by_alias or key in g.countries or key in g.subdivisions


def _tokens(chunk: str, g: _Gazetteer) -> list[_Token]:
    """The places named in one chunk, in order. A hyphen separates parts ("US-CA-Menlo Park") unless it is inside a name."""
    pieces = [piece.strip() for piece in _JOINS.split(chunk) if piece.strip()]
    found: list[_Token] = []
    i = 0
    while i < len(pieces):
        pair = " ".join(pieces[i : i + 2])
        if i + 1 < len(pieces) and _is_name(pair, g):  # "Washington, DC" is one city
            found += _scan(pair.split(), g)
            i += 2
            continue
        piece = pieces[i]
        if "-" in piece and _is_name(piece, g):  # "Winston-Salem", "Guinea-Bissau"
            found += _scan(piece.replace("-", " ").split(), g)
        else:
            found += [token for part in _DASHES.split(piece) for token in _scan(part.split(), g)]
        i += 1
    return found


def _fit(reading: _Reading, places: tuple[int, ...], g: _Gazetteer) -> int:
    """2: a candidate city matches the reading; 1: a candidate is in its country but not its state; 0: none is."""
    same_country = [g.places[i] for i in places if g.places[i].country == reading.country]
    if not same_country:
        return 0
    if reading.kind == "sub" and reading.admin1 and not any(p.admin1 == reading.admin1 for p in same_country):
        return 1
    return 2


def _cues(others: list[_Token], places: tuple[int, ...], g: _Gazetteer) -> list[_Reading]:
    """The country and state the rest of the chunk states, reading each part the way the city fits."""
    chosen: list[_Reading] = []
    for token in others:
        options = [r for r in token.readings if r.kind in ("country", "alpha3", "sub", "area")]
        if not options:
            continue
        best = max(options, key=lambda r: _fit(r, places, g))
        if _fit(best, places, g) or not token.code:  # a code the city does not fit is something else (a team, a floor)
            chosen.append(best)
    return chosen


def _most_populous(indexes: list[int] | tuple[int, ...], g: _Gazetteer) -> int | None:
    return max(indexes, key=lambda i: g.places[i].population, default=None)


def _dominant(places: tuple[int, ...], g: _Gazetteer) -> int | None:
    """The one place a bare name means: the largest, if it is DOMINANT_POPULATION_RATIO times larger than the largest
    in any other country. Places of one country share a country and a name, so they are one answer.

    ponytail: a small town that shares a big city's name, with nothing else in the text, is read as the big city.
    Upgrade path: a country prior per board (the board's own country) tried before this rule.
    """
    best: dict[str, int] = {}
    for i in places:
        country = g.places[i].country
        if country not in best or g.places[i].population > g.places[best[country]].population:
            best[country] = i
    ranked = sorted(best.values(), key=lambda i: -g.places[i].population)
    if len(ranked) == 1 or g.places[ranked[0]].population >= DOMINANT_POPULATION_RATIO * g.places[ranked[1]].population:
        return ranked[0]
    return None


def _city_name(index: int, key: str, g: _Gazetteer, state: str | None = None) -> str:
    place = g.places[index]
    if place.country == "IN" and key in INDIA_CITIES:
        return INDIA_CITIES[key]
    if place.admin1 == "NY" and place.country == "US" and place.name in NYC_BOROUGHS and state in (None, "NY"):
        return "New York City"
    return place.name


def _bare_country(tokens: list[_Token], hint: str | None) -> str | None:
    """No city is named: the country the first state or country states. Two readings (CA is Canada or California) are
    settled by the rest of the chunk, then by the hint, and otherwise left open."""
    named = [{r.country for r in t.readings if r.kind in ("country", "sub", "area") and r.country} for t in tokens]
    named = [countries for countries in named if countries]
    if not named:
        return None
    countries = named[0]
    for later in named[1:]:
        if len(countries) > 1 and countries & later:
            countries = countries & later
    if len(countries) == 1:
        return next(iter(countries))
    return hint if hint in countries else None


def _resolve(tokens: list[_Token], hint: str | None, g: _Gazetteer) -> tuple[str | None, str | None]:
    """(country, city) of the first place a chunk names. Whatever the text does not settle stays None."""
    if tokens[0].readings[0].kind == "region":
        return None, None
    city = next((t for t in tokens if any(r.kind == "city" for r in t.readings)), None)
    if city is None:
        return _bare_country(tokens, hint), None
    places = next(r.places for r in city.readings if r.kind == "city")
    cues = _cues([t for t in tokens if t is not city], places, g)
    if cues:  # the text states a country, and perhaps a state: that decides, and the city must fit it
        country = cues[0].country
        state = next((c.admin1 for c in cues if c.kind == "sub" and c.country == country), None)
        index = _most_populous([i for i in places if g.places[i].country == country], g)
        return country, None if index is None else _city_name(index, city.key, g, state)
    index = _dominant(places, g)
    if index is None and hint:
        index = _most_populous([i for i in places if g.places[i].country == hint], g)
    if index is None and city.key in INDIA_CITIES:
        index = _most_populous([i for i in places if g.places[i].country == "IN"], g)
    if index is None or g.places[index].country not in g.iso2:
        return None, None
    chosen = g.places[index]
    for other in tokens:
        if other is not city and all(r.kind == "city" for r in other.readings):
            elsewhere = [g.places[i] for r in other.readings for i in r.places]
            # A larger place in another country after a smaller one ("Debar, Prishtina") reads as the region the first
            # is in, and nothing says which: unreadable. A smaller one is a list item ("London, Springfield").
            if (
                all(p.country != chosen.country for p in elsewhere)
                and max(p.population for p in elsewhere) > chosen.population
            ):
                return None, None
    return chosen.country, _city_name(index, city.key, g)


def _hint_country(hint: str | None, g: _Gazetteer) -> str | None:
    hint = (hint or "").strip()
    if len(hint) == 2 and hint.isalpha() and hint.upper() in g.iso2:
        return hint.upper()
    return g.countries.get(_fold(hint)) or g.iso3.get(hint.upper())


def parse_location(
    raw: str | None, country_hint: str | None = None, remote_hint: bool | None = None
) -> tuple[str | None, str | None, bool | None]:
    """Return (country, city, is_remote). Unknown parts are None: a place is read only when the text says it.

    The first place listed wins (places are separated by ; / | or and). Within a place, a country, state or province
    in the text decides, then the board's country hint, then a city much larger than every other of that name; what
    none of them settles stays unknown. evals/location/GUIDELINE.md is the written definition of correct.
    """
    text = (raw or "").strip()
    is_remote = remote_hint
    if is_remote is None and re.search(r"\bremote\b|work from home|\bwfh\b", text.lower()):
        is_remote = True
    g = _gazetteer()
    hint = _hint_country(country_hint, g)
    country = city = None
    for chunk in _LISTS.split(text):
        if tokens := _tokens(chunk, g):
            country, city = _resolve(tokens, hint, g)
            break
    return country or hint, city, is_remote


def title_key(title: str) -> str:
    """Lowercased words with requisition noise removed, for duplicate detection."""
    low = re.sub(r"\(.*?\)|\[.*?\]", " ", title.lower())
    low = re.sub(r"\b(req|job|id)[\s#:-]*\d+\b|\b\d{4,}\b", " ", low)
    return " ".join(_WORD.findall(low))


def dedupe_key(company: str, title: str, country: str | None, city: str | None) -> str:
    basis = "|".join(
        [" ".join(_WORD.findall(company.lower())), title_key(title), country or "?", (city or "?").lower()]
    )
    return hashlib.sha1(basis.encode()).hexdigest()


def content_hash(title: str, description: str, location_raw: str | None) -> str:
    return hashlib.sha1("\x1f".join([title, description, location_raw or ""]).encode()).hexdigest()


def work_arrangements_from(is_remote: bool | None) -> list[str]:
    """The work_arrangement values a posting permits (DATA_MODEL 8): remote is True, onsite or hybrid is False, and a
    posting that does not say (None) permits nothing we can claim, so the list is empty."""
    if is_remote is None:
        return []
    return ["remote"] if is_remote else ["onsite", "hybrid"]


# The same case-insensitive prefixes the upgrade migration used (DATA_MODEL 8), so old rows and new rows agree.
_EMPLOYMENT_PREFIXES = (
    ("full", "full_time"),
    ("part", "part_time"),
    ("contract", "contract"),
    ("intern", "internship"),
    ("temp", "temporary"),
)


def employment_type_from(raw: str | None) -> str:
    """The employment_type enum value for the board's own wording. Anything not plainly one of them is 'unknown'."""
    low = (raw or "").lower()
    return next((kind for prefix, kind in _EMPLOYMENT_PREFIXES if low.startswith(prefix)), "unknown")
