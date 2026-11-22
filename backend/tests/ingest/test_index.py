from app.ingest.adapters import map_ashby, map_greenhouse, map_lever
from app.ingest.fetch import may_close
from app.ingest.normalize import dedupe_key, html_to_text, parse_location, title_key


def test_location_recognises_indian_city_aliases():
    assert parse_location("Bangalore, Karnataka, India") == ("IN", "Bengaluru", None)
    assert parse_location("Gurgaon") == ("IN", "Gurugram", None)


def test_location_is_never_guessed():
    assert parse_location("HQ") == (None, None, None)
    assert parse_location(None) == (None, None, None)


def test_location_remote_and_hints():
    assert parse_location("Remote - India") == ("IN", None, True)
    assert parse_location("Anywhere", country_hint="in", remote_hint=False) == ("IN", None, False)
    assert parse_location("London", country_hint="United Kingdom") == ("GB", None, None)


def test_country_name_must_be_a_whole_word():
    # "ukraine" must not match "uk"
    assert parse_location("Kyiv, Ukraine")[0] is None


def test_title_key_drops_requisition_noise():
    assert title_key("Senior Backend Engineer (Payments) - Req #48213") == title_key("senior backend engineer")


def test_dedupe_key_separates_cities():
    a = dedupe_key("Acme", "Backend Engineer", "IN", "Pune")
    assert a == dedupe_key("ACME", "Backend Engineer [R-1]", "IN", "Pune")
    assert a != dedupe_key("Acme", "Backend Engineer", "IN", "Mumbai")


def test_html_to_text_handles_double_escaped_markup():
    assert html_to_text("&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;li&gt;Python&lt;/li&gt;") == "Build & ship\nPython"


def test_empty_feed_does_not_close_a_busy_source():
    assert may_close(seen=0, open_before=40) is False
    assert may_close(seen=0, open_before=2) is True
    assert may_close(seen=12, open_before=40) is True


def test_mappers_skip_incomplete_postings_and_map_fields():
    gh = map_greenhouse(
        {
            "jobs": [
                {
                    "id": 1,
                    "title": " SRE ",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Pune"},
                    "content": "&lt;p&gt;Run it&lt;/p&gt;",
                },
                {"id": 2, "title": "No link"},
            ]
        }
    )
    assert len(gh) == 1 and gh[0]["title"] == "SRE" and gh[0]["description"] == "Run it"

    lv = map_lever(
        [
            {
                "id": "a",
                "text": "PM",
                "hostedUrl": "https://y/a",
                "workplaceType": "remote",
                "categories": {"location": "Mumbai"},
                "descriptionPlain": "Own the roadmap",
                "createdAt": 1700000000000,
            }
        ]
    )
    assert lv[0]["remote_hint"] is True and lv[0]["posted_at"].year == 2023

    ab = map_ashby(
        {
            "jobs": [
                {"id": "z", "title": "Designer", "jobUrl": "https://z/z", "isListed": False},
                {
                    "id": "y",
                    "title": "Designer",
                    "jobUrl": "https://z/y",
                    "isRemote": False,
                    "address": {"postalAddress": {"addressCountry": "India"}},
                    "descriptionPlain": "Design things",
                },
            ]
        }
    )
    assert len(ab) == 1 and ab[0]["country_hint"] == "India" and ab[0]["remote_hint"] is False
