"""Scam signals in a job posting (GRD-07, INDEX decision 24): rules, not a score.

A signal names its kind and quotes the sentence of the posting that matched, so the candidate can read the reason in the
posting's own words. Nothing here is a model. The phrase lists are assumptions: a maintainer reviews them with the
Guardian gold set (evals/guardian), and a list that is too narrow only lets a posting through that the candidate still
reads; a posting that trips one pauses the campaign before any research and nothing is shared.
"""

import re
from dataclasses import dataclass

KIND_FEE = "fee_request"
KIND_DOCUMENTS = "documents_before_interview"
KIND_OFF_PLATFORM = "off_platform"
KINDS = (KIND_FEE, KIND_DOCUMENTS, KIND_OFF_PLATFORM)

# assumption: a fee or payment the candidate must make, a purchase they must fund, or money they must forward
FEE_PHRASES = re.compile(
    r"\b(?:(?:registration|application|processing|training|equipment|security|onboarding|administration|admin|"
    r"background[- ]check|starter[- ]kit)\s+(?:fee|deposit|charge|payment)|refundable\s+(?:fee|deposit)|"
    r"(?:pay|send|wire|transfer|deposit)\s+(?:us\s+|the\s+|a\s+|an\s+)?(?:small\s+)?"
    r"(?:fee|deposit|payment|money|funds)|"
    r"(?:buy|purchase)s?\s+(?:(?:your|their|his|her)\s+)?(?:own\s+)?(?:equipment|laptop|software|gift\s+cards?)|gift\s+cards?|bitcoin|"
    r"crypto(?:currency)?\s+(?:payment|wallet)|cheque\s+deposit|check\s+deposit)\b",
    re.IGNORECASE,
)
# assumption: documents and numbers an employer has no use for before an interview
DOCUMENT_TERMS = re.compile(
    r"\b(?:passport|bank\s+(?:account\s+)?(?:details|statements?|information|number)|social\s+security(?:\s+number)?|"
    r"ssn|national\s+insurance\s+number|driver'?s\s+licen[cs]e|identity\s+card|id\s+card|credit\s+card)\b",
    re.IGNORECASE,
)
DOCUMENT_REQUEST = re.compile(
    r"\b(?:send|provide|submit|upload|share|email|attach|supply|forward|give)\b", re.IGNORECASE
)
DOCUMENT_LATER = re.compile(
    r"\b(?:after\s+(?:the\s+|an\s+|your\s+)?offer|once\s+(?:you\s+are\s+)?hired|on\s+your\s+first\s+day|"
    r"upon\s+(?:hire|offer|"
    r"acceptance)|after\s+you\s+(?:accept|join|start))\b",
    re.IGNORECASE,
)
# assumption: asking the candidate to move the conversation to a chat app or a personal mailbox
OFF_PLATFORM_APPS = re.compile(
    r"\b(?:whatsapp|telegram|(?:on|via|using|through|by)\s+signal|signal\s+app|wechat)\b", re.IGNORECASE
)
OFF_PLATFORM_VERBS = re.compile(
    r"\b(?:contact|message|text|add|reach|chat|dm|write|apply|send|interview|talk|speak|ping)\b", re.IGNORECASE
)
PERSONAL_MAILBOX = re.compile(
    r"[\w.+-]+@(?:gmail|googlemail|yahoo|hotmail|outlook|aol|proton|protonmail|icloud|gmx)\.[a-z.]{2,}",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
MAX_QUOTE = 300  # characters of the matched sentence kept as the quote


@dataclass(frozen=True)
class ScamSignal:
    kind: str
    quote: str


def _quote(sentence: str) -> str:
    one_line = " ".join(sentence.split())
    return one_line if len(one_line) <= MAX_QUOTE else one_line[: MAX_QUOTE - 1] + "…"


def _kind_of(sentence: str) -> str | None:
    if FEE_PHRASES.search(sentence):
        return KIND_FEE
    if DOCUMENT_TERMS.search(sentence) and DOCUMENT_REQUEST.search(sentence) and not DOCUMENT_LATER.search(sentence):
        return KIND_DOCUMENTS
    if PERSONAL_MAILBOX.search(sentence) or (
        OFF_PLATFORM_APPS.search(sentence) and OFF_PLATFORM_VERBS.search(sentence)
    ):
        return KIND_OFF_PLATFORM
    return None


def signals(text: str) -> list[ScamSignal]:
    """One signal per matching sentence of the posting, in reading order; empty for a clean posting."""
    found: list[ScamSignal] = []
    seen: set[str] = set()
    for sentence in _SENTENCES.split(text or ""):
        kind = _kind_of(sentence)
        quote = _quote(sentence)
        if kind is not None and quote not in seen:
            seen.add(quote)
            found.append(ScamSignal(kind, quote))
    return found
