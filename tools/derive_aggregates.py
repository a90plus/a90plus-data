#!/usr/bin/env python3
"""
derive_aggregates.py

Loads ALL tournaments/*.json, computes all-time views IN MEMORY ONLY, prints them.
WRITES NOTHING to disk — this proves that per-cup-only storage is sufficient to
answer cross-tournament questions. Aggregation happens at query time, not storage time.

Usage: python derive_aggregates.py [--top N]
"""

import json
import pathlib
import argparse
import collections

ROOT = pathlib.Path(__file__).parent.parent
TOURNAMENTS_DIR = ROOT / "tournaments"

GOAL_TYPES = {"goal", "own-goal", "penalty-goal"}


def load_tournament(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def derive(top_n=10):
    files = sorted(TOURNAMENTS_DIR.glob("*.json"))
    if not files:
        print("No tournament files found in tournaments/")
        return

    # Accumulators
    player_goals = collections.defaultdict(int)       # playerId -> goals
    player_assists = collections.defaultdict(int)     # playerId -> assists
    player_cards = collections.defaultdict(int)       # playerId -> yellow cards
    player_appearances = collections.defaultdict(set) # playerId -> set of years
    player_names = {}                                 # playerId -> commonName

    country_titles = collections.defaultdict(int)     # iso3 -> titles
    country_appearances = collections.defaultdict(int)# iso3 -> appearances
    country_goals = collections.defaultdict(int)      # iso3 -> all-time goals scored
    country_names = {}                                # iso3 -> name

    years_loaded = []

    for f in files:
        data = load_tournament(f)
        year = data["year"]
        years_loaded.append(year)

        # Country appearances + titles
        for c in data.get("countries", []):
            iso3 = c["iso3"]
            country_appearances[iso3] += 1
            country_names[iso3] = c["name"]
            country_goals[iso3] += c.get("record", {}).get("goalsFor", 0)
            for p in c.get("squad", []):
                player_names[p["playerId"]] = p.get("commonName", p.get("fullName", p["playerId"]))

        if data.get("winner"):
            country_titles[data["winner"]] += 1

        # Player stats from events
        for match in data.get("matches", []):
            match_players = set()
            for event in match.get("events", []):
                pid = event.get("playerId")
                etype = event.get("type")
                if not pid:
                    continue

                match_players.add(pid)

                if etype in ("goal", "penalty-goal"):
                    player_goals[pid] += 1
                elif etype == "assist":
                    player_assists[pid] += 1
                elif etype == "yellow-card":
                    player_cards[pid] += 1

            for pid in match_players:
                player_appearances[pid].add(year)

    print("=" * 60)
    print(f"a90plus — ALL-TIME AGGREGATES (computed in memory)")
    print(f"Tournaments loaded: {sorted(years_loaded)}")
    print("=" * 60)
    print()

    print(f"TOP {top_n} ALL-TIME SCORERS")
    print("-" * 40)
    top_scorers = sorted(player_goals.items(), key=lambda x: -x[1])[:top_n]
    for rank, (pid, goals) in enumerate(top_scorers, 1):
        name = player_names.get(pid, pid)
        cups = len(player_appearances[pid])
        print(f"  {rank:2}. {name:<30} {goals:3} goals  ({cups} cup(s))")

    print()
    print(f"TOP {top_n} ALL-TIME ASSIST LEADERS")
    print("-" * 40)
    top_assists = sorted(player_assists.items(), key=lambda x: -x[1])[:top_n]
    for rank, (pid, assists) in enumerate(top_assists, 1):
        name = player_names.get(pid, pid)
        print(f"  {rank:2}. {name:<30} {assists:3} assists")

    print()
    print(f"TOP {top_n} MOST WORLD CUP APPEARANCES (matches)")
    print("-" * 40)
    all_match_apps = {}
    for pid, years_set in player_appearances.items():
        all_match_apps[pid] = len(years_set)
    top_apps = sorted(all_match_apps.items(), key=lambda x: -x[1])[:top_n]
    for rank, (pid, cups) in enumerate(top_apps, 1):
        name = player_names.get(pid, pid)
        print(f"  {rank:2}. {name:<30} {cups:3} tournament(s)")

    print()
    print("COUNTRY TITLES")
    print("-" * 40)
    top_countries = sorted(country_titles.items(), key=lambda x: -x[1])
    for iso3, titles in top_countries:
        name = country_names.get(iso3, iso3)
        apps = country_appearances[iso3]
        print(f"  {name:<25} {titles} title(s)  ({apps} tournament(s))")

    print()
    print("ALL-TIME GOALS BY COUNTRY")
    print("-" * 40)
    top_cg = sorted(country_goals.items(), key=lambda x: -x[1])[:top_n]
    for iso3, goals in top_cg:
        name = country_names.get(iso3, iso3)
        print(f"  {name:<25} {goals:4} goals")

    print()
    print("NOTE: Nothing was written to disk. Per-cup storage is sufficient.")


def main():
    parser = argparse.ArgumentParser(description="Derive all-time WC aggregates from per-cup JSON files (in memory only).")
    parser.add_argument("--top", type=int, default=10, help="Number of top entries to show per category.")
    args = parser.parse_args()
    derive(top_n=args.top)


if __name__ == "__main__":
    main()
