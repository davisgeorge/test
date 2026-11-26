import pytest

from jobbot.countries import country_name, countries_in, mentions_country, resolve_country, suggest_countries


@pytest.mark.parametrize(
    "value, code",
    [
        ("nz", "NZ"),
        ("NZ", "NZ"),
        ("New Zealand", "NZ"),
        ("new-zealand", "NZ"),
        ("UK", "GB"),
        ("England", "GB"),
        ("usa", "US"),
        ("United States of America", "US"),
        ("Deutschland", "DE"),
        ("Côte d’Ivoire", "CI"),
        ("Czechia", "CZ"),
        ("European Union", None),
        ("Narnia", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_country(value, code):
    assert resolve_country(value) == code


def test_country_name_and_suggestions():
    assert country_name("se") == "Sweden"
    assert suggest_countries("Swedn") == ["Sweden (SE)"]
    assert suggest_countries("Narnia") == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Remote, Canada; Remote, United States", {"CA", "US"}),
        ("Bangalore, India; Remote Ireland; Remote, US", {"IN", "IE", "US"}),
        ("Sydney, New South Wales, Australia", {"AU"}),
        ("Belfast, Northern Ireland", {"GB"}),
        ("Albuquerque, New Mexico", {"US"}),
        ("Atlanta, Georgia", {"US"}),  # the US state, not the country
        ("Tbilisi, Georgia", {"GE", "US"}),  # can't tell: keep both
        ("San Francisco, CA", {"US"}),
        ("Fresno, CA", {"US"}),  # CA read as California without other evidence
        ("Toronto, CA", {"CA"}),  # ... but Canada when the city says so
        ("London", {"GB"}),
        ("London, ON", {"CA"}),  # an explicit province beats the city name
        ("Berlin, DE", {"DE"}),
        ("Wilmington, DE", {"US"}),
        ("Perth, WA", {"AU"}),
        ("Seattle, WA", {"US"}),
        ("Auckland", {"NZ"}),
        ("New York or London", {"US", "GB"}),
        ("Washington, D.C.", {"US"}),
        ("Remote U.S.", {"US"}),
        ("Guinea-Bissau", {"GW"}),
        ("Remote", set()),
        ("Remote - European Union", set()),
        ("Join us", set()),  # lower-case "us" is not the country
        ("", set()),
    ],
)
def test_countries_in(text, expected):
    assert countries_in(text) == expected


def test_extra_keywords():
    assert countries_in("Hamilton") == set()
    assert countries_in("Hamilton", {"NZ": ["Hamilton"]}) == {"NZ"}
    assert mentions_country(["Remote", "Hamilton"], "NZ", {"NZ": ["Hamilton"]})
    assert not mentions_country([None, "Remote"], "NZ")
