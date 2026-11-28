"""What the two Twin model tasks return (AI_SYSTEM 8.4). Both reject extra keys, so a reply with anything else in it
fails the gateway's schema check instead of passing through."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

TOPICS = (
    "experience",
    "skills",
    "education",
    "projects",
    "availability",
    "work_authorization",
    "location",
    "expected_pay",
    "motivation",
    "current_pay",
    "contact_details",
    "other",
)
Topic = Literal[
    "experience",
    "skills",
    "education",
    "projects",
    "availability",
    "work_authorization",
    "location",
    "expected_pay",
    "motivation",
    "current_pay",
    "contact_details",
    "other",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Comments, not docstrings: a docstring becomes prose in the JSON schema the provider is sent.
class TopicClassification(_Strict):
    topic: Topic
    asks_for_restricted: bool  # pay or any way to reach the candidate, however phrased


class AnswerSentence(_Strict):
    text: str
    facts: list[str]  # aliases f1.. of the facts the sentence rests on


class TwinAnswer(_Strict):
    status: Literal["answered", "unknown"]
    sentences: list[AnswerSentence]
