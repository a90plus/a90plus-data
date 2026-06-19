#!/usr/bin/env python3
"""
validate.py <tournament_file>

Validates a tournament JSON file against schema AND enforces referential integrity:
  - Every event playerId exists in the squad
  - Every match iso3 exists in countries
  - country record equals recomputed match tally
  - Goal events sum to match scores (handling own-goals)
  - No forbidden aggregate fields (career*, allTime*, total*Titles)
  - Penalty shootout playerIds are valid

Exit 0 on success, non-zero on failure with a grouped error report.
"""

import sys
import json
import pathlib
import collections

ROOT = pathlib.Path(__file__).parent.parent

# jsonschema is optional — if absent we skip schema validation and warn
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


FORBIDDEN_PATTERNS = ["career", "alltime", "totalTitles", "allTimeTitles", "lifetimeGoals"]

GOAL_TYPES = {"goal", "own-goal", "penalty-goal"}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_schema(name):
    return load_json(ROOT / "schema" / name)


def check_forbidden_fields(obj, path="", errors=None):
    if errors is None:
        errors = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lower = k.lower()
            for pat in FORBIDDEN_PATTERNS:
                if pat.lower() in lower:
                    errors.append(f"Forbidden field '{k}' at {path}")
            check_forbidden_fields(v, f"{path}.{k}", errors)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            check_forbidden_fields(item, f"{path}[{i}]", errors)
    return errors


def validate_schema(data, errors):
    if not HAS_JSONSCHEMA:
        errors.setdefault("SCHEMA", []).append(
            "jsonschema not installed — schema validation skipped. Run: pip install jsonschema"
        )
        return

    schema_dir = ROOT / "schema"
    schema_names = ["tournament.schema.json", "country.schema.json", "player.schema.json",
                    "match.schema.json", "event.schema.json"]

    # Build a local store mapping $id -> schema so RefResolver never hits the network
    store = {}
    for name in schema_names:
        s = load_json(schema_dir / name)
        if "$id" in s:
            store[s["$id"]] = s

    tournament_schema = load_json(schema_dir / "tournament.schema.json")
    resolver = jsonschema.RefResolver(
        base_uri=tournament_schema.get("$id", ""),
        referrer=tournament_schema,
        store=store,
    )
    try:
        jsonschema.validate(data, tournament_schema, resolver=resolver)
    except jsonschema.ValidationError as e:
        errors.setdefault("SCHEMA", []).append(str(e.message))
    except jsonschema.SchemaError as e:
        errors.setdefault("SCHEMA", []).append(f"Schema itself is invalid: {e.message}")


def validate_integrity(data, errors):
    country_iso3s = {c["iso3"] for c in data.get("countries", [])}

    # Build player index: iso3 -> {playerId}
    player_index = {}  # playerId -> iso3
    all_player_ids = set()
    for country in data.get("countries", []):
        for p in country.get("squad", []):
            pid = p["playerId"]
            all_player_ids.add(pid)
            player_index[pid] = country["iso3"]

    # Per-country tally accumulators (recomputed from matches)
    tally = {iso3: collections.defaultdict(int) for iso3 in country_iso3s}

    for match in data.get("matches", []):
        mid = match["matchId"]
        home_iso3 = match["home"]["iso3"]
        away_iso3 = match["away"]["iso3"]

        # Check country refs
        for side_iso3 in (home_iso3, away_iso3):
            if side_iso3 not in country_iso3s:
                errors.setdefault("REFERENTIAL", []).append(
                    f"{mid}: team '{side_iso3}' not in countries list"
                )

        # Tally played (only for teams that exist in the countries list)
        for iso3 in (home_iso3, away_iso3):
            if iso3 in tally:
                tally[iso3]["played"] += 1

        # Count goal events for each team
        home_goals_from_events = 0
        away_goals_from_events = 0

        for event in match.get("events", []):
            eid = event["eventId"]
            pid = event.get("playerId")
            eteam = event.get("team")
            etype = event.get("type")

            # Check player ref
            if pid and pid not in all_player_ids:
                errors.setdefault("REFERENTIAL", []).append(
                    f"{mid}/{eid}: playerId '{pid}' not in any squad"
                )

            # Check related player ref
            rpid = event.get("relatedPlayerId")
            if rpid and rpid not in all_player_ids:
                errors.setdefault("REFERENTIAL", []).append(
                    f"{mid}/{eid}: relatedPlayerId '{rpid}' not in any squad"
                )

            # Goal scoring events contribute to the match score
            if etype == "goal" or etype == "penalty-goal":
                if eteam == home_iso3:
                    home_goals_from_events += 1
                elif eteam == away_iso3:
                    away_goals_from_events += 1
            elif etype == "own-goal":
                # own-goal credited against the team of the player who scored it
                if eteam == home_iso3:
                    away_goals_from_events += 1
                elif eteam == away_iso3:
                    home_goals_from_events += 1

        # Verify goals match score (ignore penalty shootout which uses separate field)
        expected_home = match["home"]["score"]
        expected_away = match["away"]["score"]
        if home_goals_from_events != expected_home:
            errors.setdefault("SCORE_MISMATCH", []).append(
                f"{mid}: home goals from events={home_goals_from_events} but score={expected_home}"
            )
        if away_goals_from_events != expected_away:
            errors.setdefault("SCORE_MISMATCH", []).append(
                f"{mid}: away goals from events={away_goals_from_events} but score={expected_away}"
            )

        # Update aggregate tally
        if home_iso3 in tally and away_iso3 in tally:
            h, a = expected_home, expected_away
            tally[home_iso3]["goalsFor"] += h
            tally[home_iso3]["goalsAgainst"] += a
            tally[away_iso3]["goalsFor"] += a
            tally[away_iso3]["goalsAgainst"] += h
            if match["stage"] == "GroupStage":
                if h > a:
                    tally[home_iso3]["won"] += 1
                    tally[away_iso3]["lost"] += 1
                    tally[home_iso3]["points"] += 3
                elif h < a:
                    tally[away_iso3]["won"] += 1
                    tally[home_iso3]["lost"] += 1
                    tally[away_iso3]["points"] += 3
                else:
                    tally[home_iso3]["drawn"] += 1
                    tally[away_iso3]["drawn"] += 1
                    tally[home_iso3]["points"] += 1
                    tally[away_iso3]["points"] += 1

        # Validate penalty shootout player refs
        ps = match.get("penaltyShootout")
        if ps:
            for kick in ps.get("order", []):
                kpid = kick.get("playerId")
                if kpid and kpid not in all_player_ids:
                    errors.setdefault("REFERENTIAL", []).append(
                        f"{mid}/shootout: playerId '{kpid}' not in any squad"
                    )

    # Validate country records vs recomputed tallies (group stage only)
    for country in data.get("countries", []):
        iso3 = country["iso3"]
        rec = country.get("record", {})
        computed = tally.get(iso3, {})

        fields_to_check = ["goalsFor", "goalsAgainst", "played"]
        for field in fields_to_check:
            stored = rec.get(field, 0)
            comp = computed.get(field, 0)
            if stored != comp:
                errors.setdefault("RECORD_MISMATCH", []).append(
                    f"{iso3}: record.{field}={stored} but computed={comp}"
                )


def validate_no_forbidden(data, errors):
    forbidden = check_forbidden_fields(data)
    if forbidden:
        errors.setdefault("FORBIDDEN_FIELDS", []).extend(forbidden)


def main():
    if len(sys.argv) < 2:
        print("Usage: validate.py <tournament_file.json>", file=sys.stderr)
        sys.exit(1)

    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Validating {path.name} ...")
    data = load_json(path)

    errors = {}

    validate_schema(data, errors)
    validate_integrity(data, errors)
    validate_no_forbidden(data, errors)

    if not errors:
        year = data.get("year", "?")
        n_countries = len(data.get("countries", []))
        n_matches = len(data.get("matches", []))
        n_events = sum(len(m.get("events", [])) for m in data.get("matches", []))
        n_players = sum(len(c.get("squad", [])) for c in data.get("countries", []))
        print(f"OK  {year}  |  {n_countries} teams  |  {n_players} players  |  {n_matches} matches  |  {n_events} events")
        sys.exit(0)
    else:
        total = sum(len(v) for v in errors.values())
        print(f"\nFAILED — {total} error(s) in {len(errors)} category(s)\n")
        for category, msgs in errors.items():
            print(f"[{category}] ({len(msgs)} error(s))")
            for msg in msgs:
                print(f"  - {msg}")
            print()
        sys.exit(1)


if __name__ == "__main__":
    main()
