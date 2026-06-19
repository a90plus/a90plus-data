"""
pytest tests for validate.py integrity rules.
Each test exercises a specific rule in isolation using minimal in-memory fixtures.
"""

import sys
import pathlib
import json
import pytest

# Add tools/ to path so we can import validate
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "tools"))
import validate as v


# ─── Minimal fixture builders ────────────────────────────────────────────────

def _player(pid, iso3, num=1, pos="FW"):
    return {
        "playerId": pid,
        "fullName": "Test Player",
        "commonName": "Test",
        "birthDate": "1990-01-01",
        "iso3": iso3,
        "shirtNumber": num,
        "position": pos,
        "club": {"name": "FC Test", "country": "Germany"},
        "captain": False
    }


def _country(iso3, squad=None, record=None, finish="GroupStage", standing=5):
    squad = squad or [_player(f"player-a-1990", iso3, 9)]
    record = record or {"played": 0, "won": 0, "drawn": 0, "lost": 0,
                        "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0, "points": 0}
    return {
        "iso3": iso3,
        "name": f"Country {iso3}",
        "confederation": "UEFA",
        "qualificationStage": "Qualifying",
        "finalStanding": standing,
        "finishStage": finish,
        "groupId": "A",
        "record": record,
        "coach": {"name": "Coach Name", "nationality": "German"},
        "squad": squad
    }


def _event(eid, mid, minute, team, pid, etype, related=None):
    return {
        "eventId": eid,
        "matchId": mid,
        "minute": minute,
        "team": team,
        "playerId": pid,
        "type": etype,
        "relatedPlayerId": related,
        "stoppageMinute": None,
        "detail": None
    }


def _match(mid, home_iso3, away_iso3, home_score, away_score, events=None, stage="GroupStage"):
    return {
        "matchId": mid,
        "stage": stage,
        "groupId": "A",
        "matchDay": 1,
        "matchNumber": 1,
        "datetime": "2022-11-20T19:00:00Z",
        "venue": {"stadium": "Stadium", "city": "City", "iso3": "QAT", "capacity": 60000},
        "home": {"iso3": home_iso3, "score": home_score, "scoreHalfTime": None, "scoreExtraTime": None, "formation": None, "startingXI": None},
        "away": {"iso3": away_iso3, "score": away_score, "scoreHalfTime": None, "scoreExtraTime": None, "formation": None, "startingXI": None},
        "afterExtraTime": False,
        "penaltyShootout": None,
        "attendance": 50000,
        "referee": {"name": "Ref Name", "nationality": "French", "iso3": "FRA", "assistants": []},
        "events": events or []
    }


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestForbiddenFields:
    def test_no_forbidden_fields_passes(self):
        data = {"goals": 5, "player": {"name": "test"}}
        errors = v.check_forbidden_fields(data)
        assert errors == []

    def test_career_field_flagged(self):
        data = {"careerGoals": 100}
        errors = v.check_forbidden_fields(data)
        assert any("careerGoals" in e for e in errors)

    def test_alltime_field_flagged(self):
        data = {"allTimeTitles": 5}
        errors = v.check_forbidden_fields(data)
        assert any("allTimeTitles" in e for e in errors)

    def test_nested_forbidden_field_flagged(self):
        data = {"player": {"career": {"goals": 10}}}
        errors = v.check_forbidden_fields(data)
        assert any("career" in e for e in errors)

    def test_total_titles_flagged(self):
        data = {"stats": {"totalTitles": 3}}
        errors = v.check_forbidden_fields(data)
        assert any("totalTitles" in e for e in errors)


class TestReferentialIntegrity:
    def test_unknown_player_in_event_flagged(self):
        pid = "known-player-1990"
        unknown_pid = "unknown-player-1990"
        country = _country("BRA", squad=[_player(pid, "BRA", 9)])
        match = _match("m1", "BRA", "ARG", 1, 0, events=[
            _event("e1-goal", "m1", 30, "BRA", unknown_pid, "goal")
        ])
        data = {"countries": [country, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "REFERENTIAL" in errors
        assert any(unknown_pid in e for e in errors["REFERENTIAL"])

    def test_known_player_in_event_passes(self):
        pid = "known-player-1990"
        country = _country("BRA", squad=[_player(pid, "BRA", 9)])
        match = _match("m1", "BRA", "ARG", 1, 0, events=[
            _event("e1-goal", "m1", 30, "BRA", pid, "goal")
        ])
        data = {"countries": [country, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "REFERENTIAL" not in errors

    def test_unknown_team_in_match_flagged(self):
        data = {
            "countries": [_country("BRA")],
            "matches": [_match("m1", "BRA", "XXX", 0, 0)]
        }
        errors = {}
        v.validate_integrity(data, errors)
        assert "REFERENTIAL" in errors
        assert any("XXX" in e for e in errors["REFERENTIAL"])


class TestScoreMismatch:
    def test_goal_events_match_score(self):
        pid = "player-a-1990"
        country_bra = _country("BRA", squad=[_player(pid, "BRA", 9)])
        match = _match("m1", "BRA", "ARG", 2, 0, events=[
            _event("e1-g1", "m1", 20, "BRA", pid, "goal"),
            _event("e2-g2", "m1", 55, "BRA", pid, "goal"),
        ])
        data = {"countries": [country_bra, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "SCORE_MISMATCH" not in errors

    def test_goal_count_mismatch_flagged(self):
        pid = "player-a-1990"
        country_bra = _country("BRA", squad=[_player(pid, "BRA", 9)])
        match = _match("m1", "BRA", "ARG", 2, 0, events=[
            _event("e1-g1", "m1", 20, "BRA", pid, "goal"),
            # only 1 goal event but score says 2
        ])
        data = {"countries": [country_bra, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "SCORE_MISMATCH" in errors

    def test_own_goal_counted_for_opponent(self):
        pid_bra = "player-bra-1990"
        pid_arg = "player-arg-1990"
        country_bra = _country("BRA", squad=[_player(pid_bra, "BRA", 9)])
        country_arg = _country("ARG", squad=[_player(pid_arg, "ARG", 10)])
        match = _match("m1", "BRA", "ARG", 0, 1, events=[
            # BRA player scores own goal → counts as ARG goal
            _event("e1-og", "m1", 45, "BRA", pid_bra, "own-goal"),
        ])
        data = {"countries": [country_bra, country_arg], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "SCORE_MISMATCH" not in errors

    def test_penalty_goal_counts_toward_score(self):
        pid = "player-a-1990"
        country = _country("FRA", squad=[_player(pid, "FRA", 10)])
        match = _match("m1", "FRA", "ARG", 1, 0, events=[
            _event("e1-pen", "m1", 80, "FRA", pid, "penalty-goal"),
        ])
        data = {"countries": [country, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "SCORE_MISMATCH" not in errors


class TestRecordMismatch:
    def test_correct_record_passes(self):
        pid = "player-a-1990"
        record = {
            "played": 1, "won": 1, "drawn": 0, "lost": 0,
            "goalsFor": 2, "goalsAgainst": 0, "goalDifference": 2, "points": 3
        }
        country_bra = _country("BRA", squad=[_player(pid, "BRA", 9)], record=record)
        country_arg = _country("ARG", record={
            "played": 1, "won": 0, "drawn": 0, "lost": 1,
            "goalsFor": 0, "goalsAgainst": 2, "goalDifference": -2, "points": 0
        })
        match = _match("m1", "BRA", "ARG", 2, 0, events=[
            _event("e1-g1", "m1", 20, "BRA", pid, "goal"),
            _event("e2-g2", "m1", 55, "BRA", pid, "goal"),
        ])
        data = {"countries": [country_bra, country_arg], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "RECORD_MISMATCH" not in errors

    def test_wrong_goals_for_flagged(self):
        pid = "player-a-1990"
        bad_record = {
            "played": 1, "won": 1, "drawn": 0, "lost": 0,
            "goalsFor": 99, "goalsAgainst": 0, "goalDifference": 99, "points": 3
        }
        country_bra = _country("BRA", squad=[_player(pid, "BRA", 9)], record=bad_record)
        match = _match("m1", "BRA", "ARG", 2, 0, events=[
            _event("e1-g1", "m1", 20, "BRA", pid, "goal"),
            _event("e2-g2", "m1", 55, "BRA", pid, "goal"),
        ])
        data = {"countries": [country_bra, _country("ARG")], "matches": [match]}
        errors = {}
        v.validate_integrity(data, errors)
        assert "RECORD_MISMATCH" in errors
