"""
Ty Jerome Live: posts to Teams every time Ty Jerome scores for the Grizzlies.

Run on a schedule (see ty-jerome-live.yml). Each run:
  1. Looks for a Grizzlies game that tips off within the next ~3 hours (or is already live).
  2. If none, exits right away.
  3. Otherwise waits for tipoff, then checks the play-by-play every POLL_SECONDS and
     posts each new made shot / free throw by Jerome to the Teams webhook.
  4. Posts a final-buzzer recap with his total, then exits.

Env vars:
  TEAMS_WEBHOOK_URL  - URL from the Teams "Send webhook alerts to a chat" workflow (blank = dry run)
  GAME_ID            - optional: watch this specific gameId (for testing)
"""

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PLAYER_ID = 1629660          # Ty Jerome
PLAYER_NAME = "Ty Jerome"
TEAM = "MEM"

POLL_SECONDS = 45
LOOKAHEAD = timedelta(hours=3, minutes=10)   # watch games tipping off within this window
MAX_RUNTIME = timedelta(hours=5, minutes=45)  # GitHub jobs are killed at 6 hours

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"
PBP_URL = "https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{gid}.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (ty-jerome-live script)",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
}

HEADLINES = [
    "🐻 TY JEROME SCORES",
    "🚨 Jerome alert 🚨",
    "💰 Ty Jerome, money",
    "🔥 Another one from Ty Jerome",
    "📣 Bucket: Ty Jerome",
]

START = datetime.now(timezone.utc)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_or_none(url):
    """Fetch JSON; return None on 403/404/network blips so the loop can keep going."""
    try:
        return fetch_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log(f"fetch failed ({e}) for {url}")
        return None


# ---------- finding the game ----------

def parse_utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def find_game(schedule, now):
    """Return (gameId, tipoff_utc, opponent, home?) for a MEM game to watch, or None."""
    for day in schedule["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            home, away = g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]
            if TEAM not in (home, away) or g.get("gameStatus") == 3:
                continue
            tip = parse_utc(g["gameDateTimeUTC"])
            # upcoming within the window, or tipped off up to ~4h ago and maybe still live
            if now - timedelta(hours=4) <= tip <= now + LOOKAHEAD:
                is_home = home == TEAM
                return g["gameId"], tip, (away if is_home else home), is_home
    return None


# ---------- formatting ----------

def fmt_clock(iso_clock):
    """'PT04M31.00S' -> '4:31'"""
    m = re.match(r"PT(\d+)M([\d.]+)S", iso_clock or "")
    if not m:
        return ""
    return f"{int(m.group(1))}:{int(float(m.group(2))):02d}"


def fmt_period(p):
    return f"Q{p}" if p <= 4 else ("OT" if p == 5 else f"{p - 4}OT")


def is_jerome_bucket(a):
    return a.get("personId") == PLAYER_ID and a.get("shotResult") == "Made"


def bucket_text(a):
    desc = a.get("description", "").strip()
    when = f"{fmt_period(a.get('period', 0))} {fmt_clock(a.get('clock'))}".strip()
    return f"{desc} — {when}"


# ---------- posting ----------

def card(title, lines):
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    body += [{"type": "TextBlock", "text": l, "wrap": True, "spacing": "Small"} for l in lines]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def post(title, lines):
    payload = card(title, lines)
    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not url:
        log(f"DRY RUN → {title} | {' / '.join(lines)}")
        return
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                log(f"posted '{title}' (HTTP {resp.status})")
                return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            log(f"post failed ({e}), retry {attempt + 1}/3")
            time.sleep(5)


# ---------- main loop ----------

def jerome_points(box):
    game = box["game"]
    team = game["homeTeam"] if game["homeTeam"]["teamTricode"] == TEAM else game["awayTeam"]
    for p in team.get("players", []):
        if p.get("personId") == PLAYER_ID:
            return p["statistics"]["points"], p.get("played") == "1"
    return 0, False


def final_recap(box, opponent):
    game = box["game"]
    mine, opp = ((game["homeTeam"], game["awayTeam"]) if game["homeTeam"]["teamTricode"] == TEAM
                 else (game["awayTeam"], game["homeTeam"]))
    result = "W" if mine["score"] > opp["score"] else "L"
    pts, played = jerome_points(box)
    line = f"{TEAM} {mine['score']}–{opp['score']} {result} vs {opponent}"
    if not played:
        return "🏁 Final — no Ty Jerome tonight", [line]
    return f"🏁 Final: Ty Jerome finished with {pts} PTS", [line]


def watch(game_id, tip, opponent):
    seen = set()
    first_pass = True
    deadline = START + MAX_RUNTIME

    wait = (tip - datetime.now(timezone.utc)).total_seconds() - 60
    if wait > 0:
        log(f"waiting {wait / 60:.0f} min for tipoff ({tip:%H:%MZ})")
        time.sleep(wait)

    while datetime.now(timezone.utc) < deadline:
        box = fetch_or_none(BOXSCORE_URL.format(gid=game_id))
        pbp = fetch_or_none(PBP_URL.format(gid=game_id))
        status = box["game"].get("gameStatus") if box else None

        if pbp:
            new = [a for a in pbp["game"].get("actions", [])
                   if is_jerome_bucket(a) and a["actionNumber"] not in seen]
            if new:
                seen.update(a["actionNumber"] for a in new)
                if first_pass and len(new) > 1:
                    # joined late (GitHub ran the job late): one catch-up message, not a flood
                    post(f"⏪ Catching up: Ty Jerome already has {len(new)} buckets",
                         [bucket_text(a) for a in new])
                else:
                    for a in new:
                        post(random.choice(HEADLINES), [bucket_text(a)])
            first_pass = False

        if status == 3:
            title, lines = final_recap(box, opponent)
            post(title, lines)
            return

        time.sleep(POLL_SECONDS)

    log("hit max runtime before the final buzzer; the next scheduled run will pick it up")


def main():
    now = datetime.now(timezone.utc)
    forced = os.environ.get("GAME_ID", "").strip()
    if forced:
        game_id, tip, opponent = forced, now, "OPP"
    else:
        found = find_game(fetch_json(SCHEDULE_URL), now)
        if not found:
            log("no Grizzlies game in this window — exiting")
            return
        game_id, tip, opponent, _ = found
    log(f"watching game {game_id} vs {opponent}, tipoff {tip:%Y-%m-%d %H:%MZ}")
    watch(game_id, tip, opponent)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error: {e}")
        sys.exit(1)
