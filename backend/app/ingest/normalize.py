"""Pure functions that turn raw posting fields into typed values.

Rule: never guess. If a value cannot be recognised it stays None, and the
matching layer treats None as "the posting does not say".
"""

import hashlib
import html
import re

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
COUNTRY_NAMES = {
    "india": "IN",
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
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
}
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


def parse_location(raw: str | None, country_hint: str | None = None, remote_hint: bool | None = None):
    """Return (country, city, is_remote). Unknown parts are None."""
    text = (raw or "").strip()
    low = text.lower()
    is_remote = remote_hint
    if is_remote is None and re.search(r"\bremote\b|work from home|\bwfh\b", low):
        is_remote = True

    country = None
    if country_hint and len(country_hint.strip()) == 2 and country_hint.strip().isalpha():
        country = country_hint.strip().upper()
    elif country_hint and country_hint.strip().lower() in COUNTRY_NAMES:
        country = COUNTRY_NAMES[country_hint.strip().lower()]

    city = None
    for part in re.split(r"[,;/|()\-–]| or | and ", low):
        part = part.strip()
        if part in INDIA_CITIES:
            city = INDIA_CITIES[part]
            country = country or "IN"
            break
    if country is None:
        for name, code in COUNTRY_NAMES.items():
            if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
                country = code
                break
    return country, city, is_remote


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
