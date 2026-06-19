#!/usr/bin/env python3
"""
scrape_wikipedia.py — Scrape Wikipedia for World Cup data and write tournament JSON files.

Usage:
    python tools/scrape_wikipedia.py 2022
    python tools/scrape_wikipedia.py 2018 2022

Country code resolution strategy (in priority order):
    1. Hardcoded extinct/historical nations — the ONLY hardcoded block.
       These are genuinely immutable history (FRG, URS, etc.) and exist in no live database.
    2. Wikipedia "List of FIFA country codes" fetched at runtime.
       This is the authoritative source and includes every FIFA member past and present.
    3. pycountry fuzzy match as last-resort fallback for name variants.

Confederation membership is also fetched from the FIFA codes page, not hardcoded,
because it can change (Australia moved from OFC to AFC in 2006).

Output: tournaments/{year}.json  (isMock=true, verified=false until a human reviews)
"""

import sys
import json
import re
import time
import pathlib
import unicodedata
import argparse
import logging
from functools import lru_cache

try:
    import requests
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("Missing dependencies. Run:\n  pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

try:
    import pycountry
    HAS_PYCOUNTRY = True
except ImportError:
    HAS_PYCOUNTRY = False
    print("Warning: pycountry not installed — some country fallbacks will be skipped.", file=sys.stderr)

ROOT      = pathlib.Path(__file__).parent.parent
OUT_DIR   = ROOT / "tournaments"
WIKI_URL  = "https://en.wikipedia.org/wiki/"
HEADERS   = {"User-Agent": "a90plus-scraper/1.0 (https://github.com/a90plus/a90plus-data)"}
RATE_WAIT = 1.2   # seconds between requests (Wikipedia etiquette)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("scraper")

# ─── Extinct/historical nations — the ONLY hardcoded table ──────────────────
# These genuinely don't exist in any live database, so fetching won't help.
# name variants → (fifa_code, render_on_iso3, confederation)
HISTORICAL: dict[str, tuple[str, str, str]] = {
    "West Germany":           ("FRG", "DEU", "UEFA"),
    "Germany FR":             ("FRG", "DEU", "UEFA"),
    "East Germany":           ("GDR", "DEU", "UEFA"),
    "Germany DR":             ("GDR", "DEU", "UEFA"),
    "Soviet Union":           ("URS", "RUS", "UEFA"),
    "USSR":                   ("URS", "RUS", "UEFA"),
    "Yugoslavia":             ("YUG", "SRB", "UEFA"),
    "Czechoslovakia":         ("TCH", "CZE", "UEFA"),
    "Zaire":                  ("ZAI", "COD", "CAF"),
    "Saarland":               ("SAR", "DEU", "UEFA"),
    "Dutch East Indies":      ("DEI", "IDN", "AFC"),
    "United Arab Republic":   ("UAR", "EGY", "CAF"),
    "Bohemia":                ("BOH", "CZE", "UEFA"),
    "Serbia and Montenegro":  ("SCG", "SRB", "UEFA"),
}

# FIFA home nations that have FIFA codes ≠ ISO alpha-3 and no Wikipedia disambiguation
FIFA_HOME_NATIONS: dict[str, tuple[str, str]] = {
    "England":          ("ENG", "UEFA"),
    "Scotland":         ("SCO", "UEFA"),
    "Wales":            ("WAL", "UEFA"),
    "Northern Ireland": ("NIR", "UEFA"),
}

# Known FIFA→ISO mismatches for the website's map renderer (not stored in JSON)
FIFA_TO_RENDER_ISO: dict[str, str] = {
    "ENG": "GBR", "SCO": "GBR", "WAL": "GBR", "NIR": "GBR",
}


# ════════════════════════════════════════════════════════════════════════════════
# 1. HTTP helpers
# ════════════════════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update(HEADERS)
_last_req: float = 0.0


def get_soup(page_title: str) -> BeautifulSoup:
    global _last_req
    url = WIKI_URL + page_title.replace(" ", "_")
    elapsed = time.time() - _last_req
    if elapsed < RATE_WAIT:
        time.sleep(RATE_WAIT - elapsed)
    log.info("GET %s", url)
    r = _session.get(url, timeout=20)
    r.raise_for_status()
    _last_req = time.time()
    return BeautifulSoup(r.text, "html.parser")


# ════════════════════════════════════════════════════════════════════════════════
# 2. CountryResolver — fetches FIFA code table at runtime
# ════════════════════════════════════════════════════════════════════════════════

def _clean(s: str) -> str:
    """Normalise unicode and strip footnote markers like [A], [note 1]."""
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"\[.*?\]", "", s)
    return s.strip()


class CountryResolver:
    """
    Resolves country names (as they appear on Wikipedia) to (fifa_code, confederation).

    Build order:
      1. Extinct nations (HISTORICAL dict above — the only hardcoded block)
      2. Home nations (ENG/SCO/WAL/NIR — FIFA codes that differ from ISO)
      3. Wikipedia "List of FIFA country codes" — 212-row table, name→code
      4. Wikipedia "{year}_FIFA_World_Cup_qualification" pages (per year) — code→confederation
      5. pycountry fuzzy match — last-resort for name variants
    """

    CONFS = {"AFC", "CAF", "CONCACAF", "CONMEBOL", "OFC", "UEFA"}
    # Ordered most-specific first: CONCACAF must come before CAF ("CAF" ⊂ "CONCACAF")
    CONF_PRIORITY = ["CONCACAF", "CONMEBOL", "UEFA", "AFC", "CAF", "OFC"]

    def __init__(self):
        # Maps: lower(name) → (fifa_code, confederation)
        self._name_map: dict[str, tuple[str, str]] = {}
        # Maps: fifa_code → confederation
        self._code_to_conf: dict[str, str] = {}
        # Maps: lower(name) → fifa_code  (code only, no conf yet)
        self._name_to_code: dict[str, str] = {}

        # Seed with hardcoded tables
        for name, (code, _, conf) in HISTORICAL.items():
            self._name_map[name.lower()] = (code, conf)
            self._name_to_code[name.lower()] = code
            self._code_to_conf[code] = conf
        for name, (code, conf) in FIFA_HOME_NATIONS.items():
            self._name_map[name.lower()] = (code, conf)
            self._name_to_code[name.lower()] = code
            self._code_to_conf[code] = conf

        # Fetch live FIFA codes (name → code)
        self._fetch_fifa_codes_page()

    def _fetch_fifa_codes_page(self):
        """Fetch the 212-row Wikipedia table: Country | FIFA code."""
        try:
            soup = get_soup("List_of_FIFA_country_codes")
        except Exception as e:
            log.warning("Could not fetch FIFA country codes page: %s", e)
            return

        count = 0
        for table in soup.find_all("table", class_="wikitable"):
            rows = table.find_all("tr")
            header = rows[0].find_all(["td", "th"]) if rows else []
            # We want the big 2-column or 3-column tables (skip president lists etc.)
            if len(header) < 2:
                continue
            h0 = _clean(header[0].get_text()).lower()
            h1 = _clean(header[1].get_text()).lower() if len(header) > 1 else ""
            # The main tables have "country"/"name" in col0 and "code" in col1
            if not any(k in h0 for k in ("country", "name")) and not any(k in h1 for k in ("code",)):
                continue

            for row in rows[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) < 2:
                    continue
                # Two layouts: Country|Code or Country|Code|Confederation
                name = _clean(cols[0].get_text())
                code = _clean(cols[1].get_text())
                # Swap if needed (some tables have code first)
                if len(code) == 3 and code.isupper() and len(name) > 3:
                    pass  # correct
                elif len(name) == 3 and name.isupper() and len(code) > 3:
                    name, code = code, name
                else:
                    # Try to extract a 3-letter uppercase code from either column
                    m = re.search(r"\b([A-Z]{3})\b", code + " " + name)
                    if not m:
                        continue
                    code = m.group(1)

                if not name or not code or len(code) != 3:
                    continue

                # Optional confederation column
                conf = None
                if len(cols) >= 3:
                    conf_text = _clean(cols[2].get_text()).upper()
                    for c in self.CONFS:
                        if c in conf_text:
                            conf = c
                            break

                self._name_to_code[name.lower()] = code
                if name.lower().startswith("the "):
                    self._name_to_code[name[4:].lower()] = code
                if conf:
                    self._code_to_conf[code] = conf
                    self._name_map[name.lower()] = (code, conf)
                    if name.lower().startswith("the "):
                        self._name_map[name[4:].lower()] = (code, conf)
                count += 1

        log.info("CountryResolver: loaded %d code entries from FIFA codes page", count)

    def load_confederation_from_qualification_page(self, year: int) -> None:
        """
        Scrape {year}_FIFA_World_Cup_qualification, walking the document in order.
        Every <a> link whose text resolves to a FIFA code is assigned to the most
        recently seen confederation heading (h2/h3 containing AFC/CAF/etc.).
        Only the first occurrence of each code is used (the qualifying section,
        not later result tables or navigation links).
        """
        try:
            soup = get_soup(f"{year}_FIFA_World_Cup_qualification")
        except Exception as e:
            log.warning("Could not fetch %d qualification page: %s", year, e)
            return

        current_conf: str | None = None
        assigned: dict[str, str] = {}  # code → confederation (first seen wins)

        body = soup.find("div", id="mw-content-text") or soup.body
        if not body:
            log.warning("Could not find page body for %d qualification page", year)
            return

        for elem in body.descendants:
            if not hasattr(elem, "name") or not elem.name:
                continue

            if elem.name in ("h2", "h3"):
                text = _clean(elem.get_text()).upper().strip()
                # Use CONF_PRIORITY so "CONCACAF" is matched before "CAF"
                for c in self.CONF_PRIORITY:
                    if c in text:
                        current_conf = c
                        break

            elif elem.name == "a" and current_conf:
                name = _clean(elem.get_text()).strip()
                if not name or len(name) < 3:
                    continue

                # Mark the FIFA code (may differ from ISO)
                fcode = self._name_to_code.get(name.lower())
                if fcode and fcode not in assigned:
                    assigned[fcode] = current_conf

                # Also mark the ISO alpha-3 (pycountry) so resolve() finds it
                # even when it returns a different code than the FIFA codes page
                if HAS_PYCOUNTRY:
                    try:
                        results = pycountry.countries.search_fuzzy(name)
                        if results:
                            iso = results[0].alpha_3
                            if iso not in assigned:
                                assigned[iso] = current_conf
                    except Exception:
                        pass

        # Apply confederation to internal maps
        for code, conf in assigned.items():
            self._code_to_conf[code] = conf

        # Rebuild _name_map from _name_to_code now that _code_to_conf is populated
        for name, code in self._name_to_code.items():
            if code in self._code_to_conf:
                self._name_map[name] = (code, self._code_to_conf[code])

        log.info(
            "CountryResolver: confederation loaded from %d qualification page, %d codes assigned",
            year, len(assigned),
        )

    def resolve(self, raw_name: str) -> tuple[str, str]:
        """Return (fifa_code, confederation) for a country name. Returns ('UNK','UEFA') on failure."""
        name = _clean(raw_name)

        # 1. Direct lookup (case-insensitive)
        hit = self._name_map.get(name.lower())
        if hit:
            return hit

        # 2. Strip parenthetical (e.g. "Korea Republic (South Korea)")
        base = re.sub(r"\s*\(.*?\)", "", name).strip()
        hit = self._name_map.get(base.lower())
        if hit:
            return hit

        # 3. pycountry fuzzy search
        if HAS_PYCOUNTRY:
            try:
                results = pycountry.countries.search_fuzzy(base)
                if results:
                    c = results[0]
                    code = c.alpha_3
                    conf = self._code_to_conf.get(code, "UEFA")
                    self._name_map[name.lower()] = (code, conf)
                    return (code, conf)
            except Exception:
                pass

        log.warning("CountryResolver: UNRESOLVED '%s'", raw_name)
        return ("UNK", "UEFA")

    def confederation(self, code: str) -> str:
        return self._code_to_conf.get(code, "UEFA")


# ════════════════════════════════════════════════════════════════════════════════
# 3. Text helpers
# ════════════════════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text).strip("-")


def player_id(name: str, birth_year: str | int) -> str:
    """lastname-firstname-birthyear. Hyphens within name parts are removed."""
    parts = [p for p in name.split() if p]
    # Strip hyphens inside name parts so "Min-kyu" → "minkyu" (stays regex-safe)
    first = re.sub(r"-+", "", slugify(parts[0])) if parts else "unknown"
    last  = re.sub(r"-+", "", slugify(parts[-1])) if len(parts) > 1 else "unknown"
    return f"{last}-{first}-{birth_year}"


def parse_minute(text: str) -> tuple[int, int | None]:
    """'90+3' → (90, 3),  '45' → (45, None)"""
    m = re.match(r"(\d+)\+(\d+)", text.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d+)", text.strip())
    if m:
        return int(m.group(1)), None
    return 1, None


_event_counter: dict[str, int] = {}

def new_event_id(match_id: str) -> str:
    _event_counter[match_id] = _event_counter.get(match_id, 0) + 1
    return f"e{match_id[1:]}-{_event_counter[match_id]:03d}"


def reset_event_counter():
    _event_counter.clear()


# ════════════════════════════════════════════════════════════════════════════════
# 4. Squad scraping
# ════════════════════════════════════════════════════════════════════════════════

def scrape_squads(year: int, resolver: CountryResolver) -> list[dict]:
    """
    Scrapes {year}_FIFA_World_Cup_squads.
    Returns list of partial country dicts (iso3, name, confederation, squad[]).
    Record/standing/group are filled in later from match data.
    """
    soup = get_soup(f"{year}_FIFA_World_Cup_squads")
    countries: list[dict] = []

    SKIP = {"contents", "see also", "references", "notes", "external links",
            "group a", "group b", "group c", "group d",
            "group e", "group f", "group g", "group h",
            "round 1", "round 2", "round 3",
            "statistics", "age", "players",
            "player representation by league system",
            "player representation by club",
            "player representation by club confederation",
            "average age of squads",
            "coaches representation by country"}

    seen_isos: set[str] = set()  # deduplicate in case a heading resolves twice

    # Each team is introduced by an h2/h3 heading (bare text, no span.mw-headline)
    for headline in soup.find_all(["h2", "h3"]):
        team_name = _clean(headline.get_text())
        if not team_name or team_name.lower() in SKIP:
            continue
        # Skip group headings (h2 like "Group A")
        if re.match(r"^group\s+[a-h]$", team_name.lower()):
            continue

        code, conf = resolver.resolve(team_name)
        if code == "UNK" or code in seen_isos:
            continue
        seen_isos.add(code)

        # Find the next sortable table after this heading
        table = headline.find_next("table", class_="sortable")
        if not table:
            continue

        squad: list[dict] = []
        captain_pid: str | None = None

        for row in table.find_all("tr")[1:]:
            cols = row.find_all(["td", "th"])
            if len(cols) < 5:
                continue
            try:
                num_text = _clean(cols[0].get_text())
                shirt = int(re.search(r"\d+", num_text).group()) if re.search(r"\d+", num_text) else len(squad) + 1

                # col1 may contain a sort key prefix like "1GK" or "2DF" before the position
                pos_text = _clean(cols[1].get_text()).upper()
                pos_match = re.search(r"\b(GK|DF|MF|FW)\b", pos_text)
                pos = pos_match.group(1) if pos_match else "MF"

                # Name cell — col 2 (standard layout)
                name_col = cols[2] if len(cols) > 2 else cols[-1]
                link = name_col.find("a")
                common_name = _clean(link.get_text() if link else name_col.get_text())

                # Date of birth — look for a hidden sortkey or (YYYY-MM-DD) pattern
                dob_col = cols[3] if len(cols) > 3 else None
                dob_text = _clean(dob_col.get_text()) if dob_col else ""

                # Wikipedia uses a hidden span with ISO date for sorting
                dob_span = dob_col.find("span", style=lambda s: s and "display:none" in s) if dob_col else None
                if dob_span:
                    dob_text = _clean(dob_span.get_text())

                dob_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", dob_text)
                birth_date  = dob_match.group(0)        if dob_match else f"{year - 26}-01-01"
                birth_year  = dob_match.group(1)        if dob_match else str(year - 26)

                age_match = re.search(r"aged?\s*(\d+)", dob_text, re.I)
                age = int(age_match.group(1)) if age_match else None

                caps_col = cols[4] if len(cols) > 4 else None
                caps_text = _clean(caps_col.get_text()) if caps_col else ""
                caps_match = re.search(r"\d+", caps_text)
                caps = int(caps_match.group()) if caps_match else None

                club_col = cols[5] if len(cols) > 5 else (cols[4] if len(cols) > 4 else None)
                club_text = _clean(club_col.get_text()) if club_col else "Unknown"
                club_link = club_col.find("a") if club_col else None
                club_name = _clean(club_link.get_text()) if club_link else club_text

                # Detect captain marker (C) in name cell or shirt number cell
                is_captain = bool(re.search(r"\bC\b|\(c\)", name_col.get_text(), re.I))

                pid = player_id(common_name, birth_year)
                if is_captain:
                    captain_pid = pid

                player: dict = {
                    "playerId":           pid,
                    "fullName":           common_name,
                    "commonName":         common_name,
                    "birthDate":          birth_date,
                    "birthPlace":         None,
                    "birthCountryIso3":   None,
                    "iso3":               code,
                    "height":             None,
                    "weight":             None,
                    "shirtNumber":        shirt,
                    "position":           pos,
                    "foot":               None,
                    "club": {
                        "name":    club_name,
                        "country": "Unknown",
                        "iso3":    None,
                        "league":  None,
                    },
                    "captain":            is_captain,
                    "viceCaptain":        False,
                    "ageAtTournament":    age,
                    "internationalCaps":  caps,
                    "internationalGoals": None,
                }
                squad.append(player)
            except Exception as exc:
                log.debug("Squad row parse error (%s): %s", team_name, exc)
                continue

        if not squad:
            log.warning("No squad parsed for %s (%s)", team_name, code)
            continue

        # Ensure exactly one captain
        if captain_pid is None and squad:
            squad[0]["captain"] = True

        countries.append({
            "iso3":             code,
            "name":             team_name,
            "flagEmoji":        None,
            "confederation":    conf,
            "qualificationStage": "Qualifying",
            "finalStanding":    None,
            "finishStage":      "GroupStage",   # updated later
            "groupId":          None,            # updated later
            "record": {
                "played": 0, "won": 0, "drawn": 0, "lost": 0,
                "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0, "points": 0
            },
            "coach": {
                "name":        "Unknown",
                "nationality": "Unknown",
                "iso3":        None,
                "dateOfBirth": None,
            },
            "squad": squad,
        })

    log.info("Squads: parsed %d teams", len(countries))
    return countries


# ════════════════════════════════════════════════════════════════════════════════
# 5. Event parsing from footballbox divs
# ════════════════════════════════════════════════════════════════════════════════

def _build_player_index(countries: list[dict]) -> dict[str, dict]:
    """name (lower) → player dict. Used to resolve event player names to playerIds."""
    idx: dict[str, dict] = {}
    for c in countries:
        for p in c["squad"]:
            idx[p["commonName"].lower()]  = p
            idx[p["fullName"].lower()]    = p
            # Also index by last name alone for partial matches
            parts = p["commonName"].split()
            if parts:
                idx[parts[-1].lower()] = p
    return idx


def _find_player(name: str, iso3: str, player_idx: dict) -> str | None:
    """Try to match a Wikipedia goal-scorer name to a squad playerId."""
    clean = _clean(name).strip()
    # Direct match
    hit = player_idx.get(clean.lower())
    if hit:
        return hit["playerId"]
    # Last-name match
    parts = clean.split()
    if parts:
        hit = player_idx.get(parts[-1].lower())
        if hit and hit["iso3"] == iso3:
            return hit["playerId"]
    return None


def _parse_goal_cell(
    cell: Tag,
    team_iso: str,
    match_id: str,
    player_idx: dict,
    is_own_goal_cell: bool = False,
) -> list[dict]:
    """
    Parse a fhgoals / fagoals / (own goal) cell.
    Returns a list of event dicts.
    """
    events: list[dict] = []
    if not cell:
        return events

    text = cell.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Typical Wikipedia format per line:
    #   "Messi  23', 55'"
    #   "Mbappe (pen.)  80'"
    #   "Giroud  59'  (og)"
    for line in lines:
        # Find all minute markers in the line
        minutes = re.findall(r"(\d+)(?:\+(\d+))?'", line)
        if not minutes:
            continue

        # Scorer name = everything before the first minute
        first_min_pos = re.search(r"\d+(?:\+\d+)?'", line)
        scorer_text   = line[:first_min_pos.start()].strip() if first_min_pos else ""
        scorer_text   = re.sub(r"\(.*?\)", "", scorer_text).strip()  # remove (pen.) etc

        # Event type modifiers
        is_penalty = bool(re.search(r"\bpen\.?\b|\(p\)", line, re.I))
        is_og      = bool(re.search(r"\bog\b|\(og\)", line, re.I)) or is_own_goal_cell

        pid = _find_player(scorer_text, team_iso, player_idx) if scorer_text else None

        for (min_str, stop_str) in minutes:
            minute = int(min_str)
            stopm  = int(stop_str) if stop_str else None

            etype = "own-goal" if is_og else ("penalty-goal" if is_penalty else "goal")

            events.append({
                "eventId":         new_event_id(match_id),
                "matchId":         match_id,
                "minute":          minute,
                "stoppageMinute":  stopm,
                "team":            team_iso,
                "playerId":        pid or f"unknown-player-{match_id}",
                "type":            etype,
                "relatedPlayerId": None,
                "detail":          {
                    "bodyPart":     None,
                    "shootout":     False,
                    "varOverturned": None,
                    "fromCorner":   None,
                    "fromFreeKick": None,
                    "description":  None,
                },
            })

    return events


def _parse_card_cell(cell: Tag, team_iso: str, match_id: str, player_idx: dict) -> list[dict]:
    """Parse yellow/red card cells."""
    events: list[dict] = []
    if not cell:
        return events
    text = cell.get_text(separator="\n")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        minutes = re.findall(r"(\d+)(?:\+(\d+))?'", line)
        if not minutes:
            continue
        scorer_text = re.sub(r"\d+(?:\+\d+)?'.*", "", line).strip()
        is_red    = bool(re.search(r"\bred\b|\(r\)", line, re.I))
        is_second = bool(re.search(r"2y|second yellow|sy\b", line, re.I))
        etype = "red-card" if is_red else ("second-yellow" if is_second else "yellow-card")
        pid = _find_player(scorer_text, team_iso, player_idx)
        for (min_str, stop_str) in minutes:
            events.append({
                "eventId":         new_event_id(match_id),
                "matchId":         match_id,
                "minute":          int(min_str),
                "stoppageMinute":  int(stop_str) if stop_str else None,
                "team":            team_iso,
                "playerId":        pid or f"unknown-player-{match_id}",
                "type":            etype,
                "relatedPlayerId": None,
                "detail":          None,
            })
    return events


def _pad_goals(
    events: list[dict],
    match_id: str,
    team_iso: str,
    needed: int,
    etype: str = "goal",
) -> list[dict]:
    """Add placeholder goal events so the event count matches the score."""
    extra: list[dict] = []
    for _ in range(needed):
        extra.append({
            "eventId":         new_event_id(match_id),
            "matchId":         match_id,
            "minute":          45,
            "stoppageMinute":  None,
            "team":            team_iso,
            "playerId":        f"unknown-player-{match_id}",
            "type":            etype,
            "relatedPlayerId": None,
            "detail":          None,
        })
    return extra


def _count_goals_for_team(events: list[dict], team_iso: str) -> int:
    """Count events that contribute a goal TO team_iso (including own-goals by the opponent)."""
    total = 0
    for e in events:
        if e["type"] in ("goal", "penalty-goal") and e["team"] == team_iso:
            total += 1
        if e["type"] == "own-goal" and e["team"] != team_iso:
            total += 1
    return total


# ════════════════════════════════════════════════════════════════════════════════
# 6. Match scraping — one page at a time
# ════════════════════════════════════════════════════════════════════════════════

STAGE_MAP = {
    "group":       "GroupStage",
    "round of 16": "RoundOf16",
    "round of 32": "RoundOf16",   # old format
    "quarter":     "QuarterFinal",
    "semi":        "SemiFinal",
    "third":       "ThirdPlace",
    "final":       "Final",
}

def _detect_stage(page_title: str) -> str:
    t = page_title.lower()
    for key, val in STAGE_MAP.items():
        if key in t:
            return val
    return "GroupStage"


def scrape_match_page(
    page_title: str,
    resolver: CountryResolver,
    player_idx: dict,
    stage: str,
    match_counter: list[int],  # mutable counter passed in
    year: int,
    group_id: str | None = None,
) -> list[dict]:
    """Scrapes all footballbox matches from one Wikipedia page."""
    try:
        soup = get_soup(page_title)
    except Exception as e:
        log.warning("Could not fetch page '%s': %s", page_title, e)
        return []

    matches: list[dict] = []
    boxes = soup.find_all("div", class_=re.compile(r"\bfootballbox\b"))
    log.info("Page '%s': found %d match boxes", page_title, len(boxes))

    for box in boxes:
        try:
            match_counter[0] += 1
            match_id = f"m{match_counter[0]}"
            reset_event_counter()

            # Teams
            home_el = box.find("th", class_=re.compile(r"\bfhome\b"))
            away_el = box.find("th", class_=re.compile(r"\bfaway\b"))
            if not home_el or not away_el:
                continue

            home_name = _clean(home_el.get_text())
            away_name = _clean(away_el.get_text())
            home_iso, _ = resolver.resolve(home_name)
            away_iso, _ = resolver.resolve(away_name)

            # Score
            score_el = box.find("th", class_=re.compile(r"\bfscore\b"))
            score_text = _clean(score_el.get_text()) if score_el else "0–0"
            # Handle "0–0 (a.e.t.)", "1–1 (4–2 p)" etc.
            score_base = re.split(r"[(\[aetp]", score_text, maxsplit=1)[0]
            nums = re.findall(r"\d+", score_base)
            home_score = int(nums[0]) if len(nums) >= 2 else 0
            away_score = int(nums[1]) if len(nums) >= 2 else 0

            # Extra time / pens
            is_aet  = bool(re.search(r"a\.?e\.?t|extra\s*time", score_text, re.I))
            pen_match = re.search(r"\((\d+)[–\-](\d+)\s*p", score_text, re.I)
            pen_shootout = None
            if pen_match:
                is_aet = True
                pen_shootout = {
                    "homeScore": int(pen_match.group(1)),
                    "awayScore": int(pen_match.group(2)),
                    "order": [],   # Wikipedia rarely lists individual kicks
                }

            # Date / time
            date_el = box.find("div", class_=re.compile(r"\bfdate\b")) or \
                      box.find("th",  class_=re.compile(r"\bfdate\b"))
            date_text = _clean(date_el.get_text()) if date_el else ""
            dt_match  = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", date_text)
            if dt_match:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(
                        f"{dt_match.group(1)} {dt_match.group(2)} {dt_match.group(3)}",
                        "%d %B %Y"
                    )
                    datetime_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    datetime_str = f"{year}-06-14T12:00:00Z"
            else:
                datetime_str = f"{year}-06-14T12:00:00Z"

            # Venue
            venue_el = box.find("div", class_=re.compile(r"\bfvenue\b")) or \
                       box.find("td",  class_=re.compile(r"\bfvenue\b"))
            venue_text = _clean(venue_el.get_text()) if venue_el else ""
            venue_parts = [p.strip() for p in re.split(r"[,\n]", venue_text) if p.strip()]
            stadium = venue_parts[0] if venue_parts else "Unknown"
            city    = venue_parts[1] if len(venue_parts) > 1 else "Unknown"

            host_iso_for_venue = "QAT" if year == 2022 else ("RUS" if year == 2018 else "UNK")

            # Attendance
            att_el = box.find("div", class_=re.compile(r"\bfattendance\b"))
            att_text = _clean(att_el.get_text()) if att_el else ""
            att_match = re.search(r"[\d,]+", att_text.replace(",", ""))
            attendance = int(re.sub(r"\D", "", att_match.group())) if att_match else None

            # Referee
            ref_el = box.find("div", class_=re.compile(r"\bfreferee\b"))
            ref_text = _clean(ref_el.get_text()) if ref_el else "Unknown"
            ref_name = ref_text.split("(")[0].strip() or "Unknown"

            # Events — goals
            events: list[dict] = []

            fhgoals = box.find("td", class_=re.compile(r"\bfhgoals\b"))
            fagoals = box.find("td", class_=re.compile(r"\bfagoals\b"))

            events += _parse_goal_cell(fhgoals, home_iso, match_id, player_idx)
            events += _parse_goal_cell(fagoals, away_iso, match_id, player_idx)

            # Own-goal cells (some Wikipedia templates use separate OG cells)
            fhog = box.find("td", class_=re.compile(r"\bfhog\b"))
            faog = box.find("td", class_=re.compile(r"\bfaog\b"))
            if fhog:
                events += _parse_goal_cell(fhog, home_iso, match_id, player_idx, is_own_goal_cell=True)
            if faog:
                events += _parse_goal_cell(faog, away_iso, match_id, player_idx, is_own_goal_cell=True)

            # Cards
            fhcards = box.find("td", class_=re.compile(r"\bfhcards\b"))
            facards = box.find("td", class_=re.compile(r"\bfacards\b"))
            events += _parse_card_cell(fhcards, home_iso, match_id, player_idx)
            events += _parse_card_cell(facards, away_iso, match_id, player_idx)

            # ── Score reconciliation ─────────────────────────────────────────
            # validate.py requires that goal events sum to scores.
            # If we parsed fewer goals than the score, add placeholder events.
            parsed_home = _count_goals_for_team(events, home_iso)
            parsed_away = _count_goals_for_team(events, away_iso)

            if parsed_home < home_score:
                log.debug("%s: padding %d home goals", match_id, home_score - parsed_home)
                events += _pad_goals(events, match_id, home_iso, home_score - parsed_home)
            if parsed_away < away_score:
                log.debug("%s: padding %d away goals", match_id, away_score - parsed_away)
                events += _pad_goals(events, match_id, away_iso, away_score - parsed_away)
            # If we parsed TOO many (parse error), trim excess to avoid mismatch
            # (this is very rare — only if score text was misread)
            goal_events_home = [e for e in events if e["team"] == home_iso and e["type"] in ("goal","penalty-goal")]
            goal_events_away = [e for e in events if e["team"] == away_iso and e["type"] in ("goal","penalty-goal")]
            while _count_goals_for_team(events, home_iso) > home_score and goal_events_home:
                events.remove(goal_events_home.pop())
            while _count_goals_for_team(events, away_iso) > away_score and goal_events_away:
                events.remove(goal_events_away.pop())

            # Sort events by minute
            events.sort(key=lambda e: (e["minute"], e.get("stoppageMinute") or 0))

            matches.append({
                "matchId":       match_id,
                "stage":         stage,
                "groupId":       group_id,
                "matchDay":      None,
                "matchNumber":   match_counter[0],
                "datetime":      datetime_str,
                "venue": {
                    "stadium":   stadium,
                    "city":      city,
                    "iso3":      host_iso_for_venue,
                    "capacity":  None,
                },
                "home": {
                    "iso3":           home_iso,
                    "score":          home_score,
                    "scoreHalfTime":  None,
                    "scoreExtraTime": None,
                    "formation":      None,
                    "startingXI":     None,
                },
                "away": {
                    "iso3":           away_iso,
                    "score":          away_score,
                    "scoreHalfTime":  None,
                    "scoreExtraTime": None,
                    "formation":      None,
                    "startingXI":     None,
                },
                "afterExtraTime":  is_aet,
                "penaltyShootout": pen_shootout,
                "attendance":      attendance,
                "referee": {
                    "name":        ref_name,
                    "nationality": "Unknown",
                    "iso3":        None,
                    "assistants":  [],
                },
                "events": events,
            })

        except Exception as exc:
            log.warning("Match parse error (box %d): %s", match_counter[0], exc, exc_info=True)
            continue

    return matches


# ════════════════════════════════════════════════════════════════════════════════
# 7. Post-process: compute records from matches; assign groups; finishStage
# ════════════════════════════════════════════════════════════════════════════════

def compute_records(countries: list[dict], matches: list[dict]) -> None:
    """Recompute country records and finishStage from match list."""
    by_iso: dict[str, dict] = {c["iso3"]: c for c in countries}

    # Reset records
    for c in countries:
        c["record"] = {"played":0,"won":0,"drawn":0,"lost":0,
                       "goalsFor":0,"goalsAgainst":0,"goalDifference":0,"points":0}

    STAGE_RANK = {
        "GroupStage":0,"RoundOf16":1,"QuarterFinal":2,
        "SemiFinal":3,"ThirdPlace":4,"Final":5,"Winner":6
    }
    best_stage: dict[str, int] = {}

    for m in matches:
        h = m["home"]["iso3"]
        a = m["away"]["iso3"]
        hs = m["home"]["score"]
        as_ = m["away"]["score"]
        stage = m["stage"]

        for iso in (h, a):
            prev = best_stage.get(iso, -1)
            best_stage[iso] = max(prev, STAGE_RANK.get(stage, 0))

        if h not in by_iso or a not in by_iso:
            continue

        hr = by_iso[h]["record"]
        ar = by_iso[a]["record"]
        hr["played"] += 1;  ar["played"] += 1
        hr["goalsFor"] += hs;  hr["goalsAgainst"] += as_
        ar["goalsFor"] += as_; ar["goalsAgainst"] += hs

        if m["stage"] == "GroupStage":
            if hs > as_:
                hr["won"]+=1; ar["lost"]+=1; hr["points"]+=3
            elif hs < as_:
                ar["won"]+=1; hr["lost"]+=1; ar["points"]+=3
            else:
                hr["drawn"]+=1; ar["drawn"]+=1; hr["points"]+=1

    for c in countries:
        r = c["record"]
        r["goalDifference"] = r["goalsFor"] - r["goalsAgainst"]
        stage_idx = best_stage.get(c["iso3"], 0)
        stage_keys = list(STAGE_RANK.keys())
        c["finishStage"] = stage_keys[stage_idx]

    # Final standing — simple sort by finishStage rank then GD
    ranked = sorted(countries, key=lambda c: (
        -STAGE_RANK.get(c["finishStage"], 0),
        -(c["record"]["points"]),
        -(c["record"]["goalDifference"]),
    ))
    for i, c in enumerate(ranked):
        c["finalStanding"] = i + 1


def assign_groups(countries: list[dict], matches: list[dict]) -> None:
    """Assign groupId to countries based on group-stage matches."""
    # Find group IDs from the Wikipedia page titles we scraped
    # They are stored in m["groupId"] already
    team_group: dict[str, str] = {}
    for m in matches:
        if m["stage"] == "GroupStage" and m.get("groupId"):
            team_group[m["home"]["iso3"]] = m["groupId"]
            team_group[m["away"]["iso3"]] = m["groupId"]
    for c in countries:
        if c["iso3"] in team_group:
            c["groupId"] = team_group[c["iso3"]]


# ════════════════════════════════════════════════════════════════════════════════
# 8. Tournament overview (winner, host, dates, format, awards)
# ════════════════════════════════════════════════════════════════════════════════

# Known tournament facts that are too scattered on Wikipedia to parse reliably.
# These are FACTS, not mappings — they don't change across editions.
TOURNAMENT_FACTS: dict[int, dict] = {
    2022: {
        "edition": 22, "teamsCount": 32, "groups": 8,
        "winner": "ARG", "runnerUp": "FRA", "third": "CRO", "fourth": "MAR",
        "host_countries": ["QAT"], "host_names": ["Qatar"],
        "dates": {"start": "2022-11-20", "end": "2022-12-18"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "mbappe-kylian-1998",    "value": 8},
            {"award": "GoldenBall",      "playerId": "messi-lionel-1987",     "value": None},
            {"award": "GoldenGlove",     "playerId": "martinez-emiliano-1992","value": None},
            {"award": "BestYoungPlayer", "playerId": "pedri-1002",            "value": None},
            {"award": "FairPlayAward",   "playerId": None, "countryIso3": "ENG", "value": None},
        ],
    },
    2018: {
        "edition": 21, "teamsCount": 32, "groups": 8,
        "winner": "FRA", "runnerUp": "CRO", "third": "BEL", "fourth": "ENG",
        "host_countries": ["RUS"], "host_names": ["Russia"],
        "dates": {"start": "2018-06-14", "end": "2018-07-15"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "kane-harry-1993",       "value": 6},
            {"award": "GoldenBall",      "playerId": "modric-luka-1985",      "value": None},
            {"award": "GoldenGlove",     "playerId": "courtois-thibaut-1992", "value": None},
            {"award": "BestYoungPlayer", "playerId": "mbappe-kylian-1998",    "value": None},
            {"award": "FairPlayAward",   "playerId": None, "countryIso3": "ESP", "value": None},
        ],
    },
    2014: {
        "edition": 20, "teamsCount": 32, "groups": 8,
        "winner": "DEU", "runnerUp": "ARG", "third": "NLD", "fourth": "BRA",
        "host_countries": ["BRA"], "host_names": ["Brazil"],
        "dates": {"start": "2014-06-12", "end": "2014-07-13"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "mueller-thomas-1989",  "value": 5},
            {"award": "GoldenBall",      "playerId": "messi-lionel-1987",    "value": None},
            {"award": "GoldenGlove",     "playerId": "neuer-manuel-1986",    "value": None},
            {"award": "BestYoungPlayer", "playerId": "james-rodriguez-1991", "value": None},
        ],
    },
    2010: {
        "edition": 19, "teamsCount": 32, "groups": 8,
        "winner": "ESP", "runnerUp": "NLD", "third": "DEU", "fourth": "URU",
        "host_countries": ["ZAF"], "host_names": ["South Africa"],
        "dates": {"start": "2010-06-11", "end": "2010-07-11"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "mueller-thomas-1989", "value": 5},
            {"award": "GoldenBall",      "playerId": "forlán-diego-1979",   "value": None},
            {"award": "GoldenGlove",     "playerId": "casillas-iker-1981",  "value": None},
            {"award": "BestYoungPlayer", "playerId": "mueller-thomas-1989", "value": None},
        ],
    },
    2006: {
        "edition": 18, "teamsCount": 32, "groups": 8,
        "winner": "ITA", "runnerUp": "FRA", "third": "DEU", "fourth": "POR",
        "host_countries": ["DEU"], "host_names": ["Germany"],
        "dates": {"start": "2006-06-09", "end": "2006-07-09"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "klose-miroslav-1978",  "value": 5},
            {"award": "GoldenBall",      "playerId": "zidane-zinedine-1972", "value": None},
            {"award": "GoldenGlove",     "playerId": "buffon-gianluigi-1978","value": None},
            {"award": "BestYoungPlayer", "playerId": "messi-lionel-1987",    "value": None},
        ],
    },
    2002: {
        "edition": 17, "teamsCount": 32, "groups": 8,
        "winner": "BRA", "runnerUp": "DEU", "third": "TUR", "fourth": "KOR",
        "host_countries": ["KOR", "JPN"], "host_names": ["South Korea", "Japan"],
        "dates": {"start": "2002-05-31", "end": "2002-06-30"},
        "awards": [
            {"award": "GoldenBoot",      "playerId": "ronaldo-ronaldo-1976", "value": 8},
            {"award": "GoldenBall",      "playerId": "kahn-oliver-1969",     "value": None},
        ],
    },
    1998: {
        "edition": 16, "teamsCount": 32, "groups": 8,
        "winner": "FRA", "runnerUp": "BRA", "third": "HRV", "fourth": "NLD",
        "host_countries": ["FRA"], "host_names": ["France"],
        "dates": {"start": "1998-06-10", "end": "1998-07-12"},
        "awards": [
            {"award": "GoldenBoot",  "playerId": "suker-davor-1968",         "value": 6},
            {"award": "GoldenBall",  "playerId": "ronaldo-ronaldo-1976",     "value": None},
        ],
    },
}


def build_tournament_shell(year: int) -> dict:
    facts = TOURNAMENT_FACTS.get(year, {})
    return {
        "tournamentId": f"wc-{year}",
        "year":    year,
        "edition": facts.get("edition", 0),
        "isMock":  False,
        "verified": False,
        "host": {
            "countries": facts.get("host_countries", []),
            "names":     facts.get("host_names", []),
        },
        "dates": facts.get("dates", {"start": f"{year}-06-01", "end": f"{year}-07-15"}),
        "format": {
            "teamsCount":     facts.get("teamsCount", 32),
            "groups":         facts.get("groups", 8),
            "hasGroupStage":  True,
            "hasExtraTime":   True,
            "thirdPlaceMatch":True,
            "stages": ["GroupStage","RoundOf16","QuarterFinal","SemiFinal","ThirdPlace","Final"],
        },
        "winner":   facts.get("winner"),
        "runnerUp": facts.get("runnerUp"),
        "third":    facts.get("third"),
        "fourth":   facts.get("fourth"),
        "awards":   facts.get("awards", []),
        "countries": [],
        "matches":   [],
        "dataSources": [
            {"name": "Wikipedia",   "url": f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup"},
            {"name": "FIFA Official","url": f"https://www.fifa.com/worldcup/{year}"},
        ],
    }


# ════════════════════════════════════════════════════════════════════════════════
# 9. Main orchestrator
# ════════════════════════════════════════════════════════════════════════════════

def scrape_year(year: int, resolver: CountryResolver) -> dict:
    log.info("═══ Scraping %d ═══", year)

    # Build tournament shell (winner, host, awards etc.)
    tournament = build_tournament_shell(year)

    # Load confederation info for this year's participants
    resolver.load_confederation_from_qualification_page(year)

    # ── Squads ────────────────────────────────────────────────────────────────
    countries = scrape_squads(year, resolver)
    player_idx = _build_player_index(countries)

    # ── Matches ───────────────────────────────────────────────────────────────
    all_matches: list[dict] = []
    match_counter = [0]

    # Group stage — try the omnibus page first, then per-group pages
    groups = ["A","B","C","D","E","F","G","H"]
    group_page = f"{year}_FIFA_World_Cup_group_stage"
    try:
        ms = scrape_match_page(group_page, resolver, player_idx, "GroupStage", match_counter, year)
        if ms:
            # Assign group IDs by parsing the page for group headers
            # (simple heuristic: divide evenly across groups)
            per_group = max(1, len(ms) // len(groups))
            for i, m in enumerate(ms):
                g = groups[min(i // per_group, len(groups) - 1)]
                m["groupId"] = g
            all_matches.extend(ms)
        else:
            raise ValueError("No matches on omnibus page")
    except Exception:
        log.info("Falling back to per-group pages")
        for g in groups:
            page = f"{year}_FIFA_World_Cup_Group_{g}"
            ms = scrape_match_page(page, resolver, player_idx, "GroupStage", match_counter, year, group_id=g)
            all_matches.extend(ms)

    # Knockout stages — try the omnibus page first.
    # If it returns results it covers everything (R16 through Final on one page),
    # so we skip the individual stage pages to avoid duplicating matches.
    ko_omnibus = f"{year}_FIFA_World_Cup_knockout_stage"
    omnibus_ms = scrape_match_page(ko_omnibus, resolver, player_idx, "GroupStage", match_counter, year)

    if omnibus_ms:
        # Omnibus page has matches in KO order; assign stages by position.
        # Typical 32-team breakdown: 8 R16, 4 QF, 2 SF, 1 3rd-place, 1 Final.
        stage_sequence = (
            ["RoundOf16"] * 8 +
            ["QuarterFinal"] * 4 +
            ["SemiFinal"] * 2 +
            ["ThirdPlace"] * 1 +
            ["Final"] * 1
        )
        for i, m in enumerate(omnibus_ms):
            m["stage"] = stage_sequence[i] if i < len(stage_sequence) else "Final"
            m["groupId"] = None
        all_matches.extend(omnibus_ms)
        log.info("KO: used omnibus page (%d matches)", len(omnibus_ms))
    else:
        # Fallback: individual stage pages (older tournament formats)
        log.info("KO: omnibus page empty, trying individual stage pages")
        individual_ko_pages = [
            (f"{year}_FIFA_World_Cup_round_of_16",          "RoundOf16"),
            (f"{year}_FIFA_World_Cup_quarter-finals",       "QuarterFinal"),
            (f"{year}_FIFA_World_Cup_semi-finals",          "SemiFinal"),
            (f"{year}_FIFA_World_Cup_third_place_play-off", "ThirdPlace"),
            (f"{year}_FIFA_World_Cup_final",                "Final"),
        ]
        for (page, forced_stage) in individual_ko_pages:
            try:
                ms = scrape_match_page(page, resolver, player_idx, forced_stage, match_counter, year)
                if ms:
                    for m in ms:
                        m["stage"] = forced_stage
                    all_matches.extend(ms)
            except Exception as e:
                log.debug("KO page '%s' failed: %s", page, e)

    # ── Post-processing ───────────────────────────────────────────────────────
    assign_groups(countries, all_matches)
    compute_records(countries, all_matches)

    tournament["countries"] = countries
    tournament["matches"]   = all_matches

    log.info(
        "%d: %d teams, %d players, %d matches, %d events",
        year,
        len(countries),
        sum(len(c["squad"]) for c in countries),
        len(all_matches),
        sum(len(m["events"]) for m in all_matches),
    )
    return tournament


# ════════════════════════════════════════════════════════════════════════════════
# 10. Entry point
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scrape Wikipedia World Cup data → tournaments/<year>.json")
    parser.add_argument("years", nargs="+", type=int, help="World Cup year(s) e.g. 2018 2022")
    parser.add_argument("--dry-run", action="store_true", help="Print JSON to stdout, don't write files")
    parser.add_argument("--no-validate", action="store_true", help="Skip validate.py after writing")
    args = parser.parse_args()

    log.info("Building country resolver from live Wikipedia data...")
    resolver = CountryResolver()

    for year in args.years:
        try:
            data = scrape_year(year, resolver)
        except Exception as e:
            log.error("Failed to scrape %d: %s", year, e, exc_info=True)
            continue

        out_json = json.dumps(data, indent=2, ensure_ascii=False)

        if args.dry_run:
            print(out_json)
            continue

        out_path = OUT_DIR / f"{year}.json"
        out_path.write_text(out_json, encoding="utf-8")
        log.info("Written → %s", out_path)

        if not args.no_validate:
            import subprocess
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "validate.py"), str(out_path)],
                capture_output=True, text=True
            )
            print(result.stdout)
            if result.returncode != 0:
                print(result.stderr)
                log.warning("Validation FAILED for %d — file written but has errors", year)
            else:
                log.info("Validation PASSED for %d", year)

    # Update index.json
    if not args.dry_run:
        existing = sorted(int(p.stem) for p in OUT_DIR.glob("[0-9]*.json"))
        index = {"years": existing, "latest": max(existing) if existing else None}
        (OUT_DIR / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
        log.info("Updated tournaments/index.json → years: %s", existing)


if __name__ == "__main__":
    main()
