"""Download historical FPL player-gameweek data for backtesting.

Source: the public vaastav/Fantasy-Premier-League repository (MIT license).
Each season directory receives merged_gw.csv, fixtures.csv, teams.csv, and
players_raw.csv plus a manifest recording source URLs and retrieval time.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen


REPO_ROOT = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
DEFAULT_SEASONS = ("2022-23", "2023-24", "2024-25", "2025-26")
SEASON_FILES = {
    "merged_gw.csv": "{root}/{season}/gws/merged_gw.csv",
    "fixtures.csv": "{root}/{season}/fixtures.csv",
    "teams.csv": "{root}/{season}/teams.csv",
    "players_raw.csv": "{root}/{season}/players_raw.csv",
}


def _download(url):
    request = Request(url, headers={"User-Agent": "fpl-intelligence/1.0"})
    with urlopen(request, timeout=120) as response:
        return response.read()


def fetch_season(season, destination):
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, template in SEASON_FILES.items():
        url = template.format(root=REPO_ROOT, season=season)
        payload = _download(url)
        (destination / name).write_bytes(payload)
        files[name] = {"source_url": url, "bytes": len(payload)}
        print(f"  {name}: {len(payload):,} bytes")
    manifest = {
        "season": season,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "license": "MIT (vaastav/Fantasy-Premier-League)",
        "files": files,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main(seasons):
    history_root = Path(__file__).resolve().parents[1] / "data" / "history"
    for season in seasons:
        print(f"Fetching {season}...")
        fetch_season(season, history_root / season)
    print("Done.")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_SEASONS)
