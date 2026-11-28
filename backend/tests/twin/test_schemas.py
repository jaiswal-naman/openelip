import pytest
from pydantic import ValidationError

from app.twin.schemas import TOPICS, TopicClassification, TwinAnswer


def test_twelve_topics_in_the_database_order() -> None:
    assert len(TOPICS) == 12 and len(set(TOPICS)) == 12


@pytest.mark.parametrize("topic", TOPICS)
def test_every_topic_is_accepted(topic: str) -> None:
    assert TopicClassification(topic=topic, asks_for_restricted=False).topic == topic


def test_topic_outside_the_twelve_is_refused() -> None:
    with pytest.raises(ValidationError):
        TopicClassification(topic="hobbies", asks_for_restricted=False)


def test_extra_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        TopicClassification.model_validate({"topic": "skills", "asks_for_restricted": False, "why": "x"})
    with pytest.raises(ValidationError):
        TwinAnswer.model_validate({"status": "unknown", "sentences": [], "note": "x"})
    with pytest.raises(ValidationError):
        TwinAnswer.model_validate({"status": "answered", "sentences": [{"text": "a", "facts": ["f1"], "x": 1}]})


def test_status_is_answered_or_unknown() -> None:
    with pytest.raises(ValidationError):
        TwinAnswer(status="maybe", sentences=[])


def test_json_schemas_carry_no_prose() -> None:
    # A docstring on a model would be sent to the provider as a description.
    for model in (TopicClassification, TwinAnswer):
        assert "description" not in model.model_json_schema()
