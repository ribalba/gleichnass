"""Places are looked up once and then remembered: towns do not move."""

from datetime import timedelta

import pytest

from gleichnass.state import Store, _key


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3") as opened:
        yield opened


KONSTANZ = [{"name": "Konstanz, Baden-Württemberg, DE", "lat": 47.66033, "lon": 9.17582}]


def test_an_unseen_query_is_a_miss(store):
    assert store.cached_places("Konstanz") is None


def test_a_remembered_query_comes_back_intact(store):
    store.remember_places("Konstanz", KONSTANZ)
    assert store.cached_places("Konstanz") == KONSTANZ


@pytest.mark.parametrize("typed", ["konstanz", "  KONSTANZ  ", "Konstanz", "kOnStAnZ"])
def test_spelling_it_differently_still_hits(store, typed):
    """Otherwise every visitor's capitalisation would be its own lookup."""
    store.remember_places("Konstanz", KONSTANZ)
    assert store.cached_places(typed) == KONSTANZ


def test_repeated_whitespace_collapses(store):
    assert _key("  Bad   Säckingen ") == "bad säckingen"


def test_an_empty_answer_is_not_remembered(store):
    """An empty list usually means the geocoder was unreachable, and caching
    that would outlast the outage."""
    store.remember_places("Konstanz", [])
    assert store.cached_places("Konstanz") is None


def test_a_later_answer_replaces_the_earlier_one(store):
    store.remember_places("Konstanz", KONSTANZ)
    store.remember_places("Konstanz", [{"name": "Konstanz", "lat": 1.0, "lon": 2.0}])
    assert store.cached_places("Konstanz")[0]["lat"] == 1.0


def test_an_entry_can_be_considered_too_old(store):
    store.remember_places("Konstanz", KONSTANZ)
    assert store.cached_places("Konstanz", max_age=timedelta(days=90)) == KONSTANZ
    assert store.cached_places("Konstanz", max_age=timedelta(seconds=0)) is None


def test_the_cache_shares_a_file_with_the_rest_of_the_state(tmp_path):
    """The site writes places while the melder writes delivery state."""
    path = tmp_path / "state.sqlite3"
    with Store(path) as site, Store(path) as melder:
        site.remember_places("Hamburg", KONSTANZ)
        melder.log_delivery("u", "night", "ntfy", "t", "b", ok=True)
        assert melder.cached_places("Hamburg") == KONSTANZ
        assert len(site.recent_deliveries()) == 1
