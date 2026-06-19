#!/usr/bin/env python3
"""
new_tournament.py <year>

Scaffolds an empty but schema-valid tournament file at tournaments/<year>.json.
The human fills in the real data; this gives a correct skeleton to start from.
"""

import sys
import json
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
OUT_DIR = ROOT / "tournaments"


def scaffold(year: int) -> dict:
    return {
        "tournamentId": f"wc-{year}",
        "year": year,
        "edition": 0,
        "isMock": True,
        "verified": False,
        "host": {
            "countries": [],
            "names": []
        },
        "dates": {
            "start": f"{year}-06-01",
            "end": f"{year}-07-15"
        },
        "format": {
            "teamsCount": 32,
            "groups": 8,
            "hasGroupStage": True,
            "hasExtraTime": True,
            "thirdPlaceMatch": True,
            "stages": ["GroupStage", "RoundOf16", "QuarterFinal", "SemiFinal", "ThirdPlace", "Final"]
        },
        "winner": None,
        "runnerUp": None,
        "third": None,
        "fourth": None,
        "awards": [
            {"award": "GoldenBoot", "playerId": None, "value": None},
            {"award": "GoldenBall", "playerId": None, "value": None},
            {"award": "GoldenGlove", "playerId": None, "value": None},
            {"award": "BestYoungPlayer", "playerId": None, "value": None},
            {"award": "FairPlayAward", "playerId": None, "countryIso3": None, "value": None}
        ],
        "countries": [],
        "matches": [],
        "dataSources": [
            {"name": "FIFA Official", "url": "https://www.fifa.com/worldcup"},
            {"name": "Wikipedia", "url": f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup"}
        ]
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: new_tournament.py <year>", file=sys.stderr)
        sys.exit(1)

    try:
        year = int(sys.argv[1])
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid year.", file=sys.stderr)
        sys.exit(1)

    if year < 1930 or year > 2050:
        print(f"Error: year {year} is out of range (1930–2050).", file=sys.stderr)
        sys.exit(1)

    out_path = OUT_DIR / f"{year}.json"
    if out_path.exists():
        print(f"Error: {out_path} already exists. Delete it first if you want to re-scaffold.", file=sys.stderr)
        sys.exit(1)

    data = scaffold(year)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Created: {out_path}")
    print(f"Next steps:")
    print(f"  1. Fill in host, dates, format, countries, matches")
    print(f"  2. Run: python tools/validate.py tournaments/{year}.json")


if __name__ == "__main__":
    main()
