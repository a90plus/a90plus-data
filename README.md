# a90plus-data

Per-tournament World Cup statistics dataset. Every file covers exactly one edition.

**No all-time aggregates are stored.** A player's career goals, a country's total titles — none of that lives here. Cross-tournament views are computed at query time by `tools/derive_aggregates.py`. This keeps the data clean, historically accurate, and free of denormalization bugs.

## Structure

```
tournaments/
  index.json          # manifest of available years
  2022.json           # 2022 World Cup (Qatar)
  2018.json           # 2018 World Cup (Russia)
schema/
  tournament.schema.json
  country.schema.json
  player.schema.json
  match.schema.json
  event.schema.json
tools/
  validate.py         # schema + referential integrity check
  derive_aggregates.py # in-memory all-time views (writes nothing)
  new_tournament.py   # scaffold a new year file
tests/
  test_validate.py
```

## Data Model

- **Tournament** — top-level file per edition
- **Country** — participation in THIS cup (record, squad, coach)
- **Player** — squad member with bio; `playerId` is the only cross-file key
- **Match** — score, venue, referee, lineup, events
- **Event** — granular action (goal/assist/card/sub/VAR). Stats are derived by counting events, never stored separately.

### Historical nations
| Code | Nation | Renders on |
|------|--------|-----------|
| FRG  | West Germany | DEU |
| GDR  | East Germany | DEU |
| URS  | Soviet Union | RUS |
| YUG  | Yugoslavia | SRB |
| TCH  | Czechoslovakia | CZE |
| ZAI  | Zaire | COD |

## Tooling

```bash
# Validate a file
python tools/validate.py tournaments/2022.json

# Compute all-time stats in memory (writes nothing)
python tools/derive_aggregates.py --top 15

# Scaffold a new tournament
python tools/new_tournament.py 2026

# Run tests
pytest tests/
```

## Contributing

1. Fork this repo
2. Edit or create `tournaments/<year>.json`
3. Run `python tools/validate.py tournaments/<year>.json` — must exit 0
4. Set `"verified": true` only after cross-checking with official FIFA match reports
5. Open a PR — the Actions workflow validates automatically

Current files are marked `"isMock": true, "verified": false`. Please help replace mock data with real figures.
