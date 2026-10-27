"""
NBA Morning Recap

Posts last night's scores to Teams, plus the Ty Jerome vs Jalen Brunson vs Wemby points battle,
and roasts the Knicks and/or Spurs if they lost.

Env vars:
  TEAMS_WEBHOOK_URL  - Teams workflow webhook URL (blank = dry run)
  GAME_DATE          - optional YYYY-MM-DD to recap (default: yesterday, US Eastern)
"""

import json
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (morning-recap script)",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
}

DUEL = [
    {"id": 1629660, "name": "Ty Jerome", "short": "Jerome", "team": "MEM"},
    {"id": 1628973, "name": "Jalen Brunson", "short": "Brunson", "team": "NYK"},
    {"id": 1641705, "name": "Victor Wembanyama", "short": "Wemby", "team": "SAS"},
]
MEDALS = ["🥇", "🥈", "🥉"]

ROASTS = {
    "NYK": [
        "🗽📉 The defending champs lost to {winner}. Banner's still up though, so that's nice.",
        "🗽 Knicks lost {score}. Somebody check if there's still parade confetti stuck in Brunson's hair.",
        "🗽 Knicks fell to {winner}. MSG is now selling 'We Were Champions In June' t-shirts at the exits.",
        "🗽 Knicks lost to {winner}. Turns out the trophy doesn't play defense.",
    ],
    "SAS": [
        "🤠📉 Spurs lost to {winner}. Wemby is 7'4\" and still couldn't reach a W.",
        "🤠 Spurs dropped one to {winner}. Still not over the Finals, apparently.",
        "🤠 Spurs lost {score}. Wemby's wingspan covers the whole court but not the loss column.",
        "🤠 Spurs lost to {winner}. The Alamo held out longer.",
    ],
}
BOTH_LOST = "📉📉 Knicks AND Spurs both lost. Finals rematch of sadness. Beautiful morning."


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_or_none(url):
    try:
        return fetch_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"fetch failed ({e}) for {url}")
        return None


def target_date():
    override = os.environ.get("GAME_DATE", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    return (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).date()


def game_date(day, g):
    est = g.get("gameDateEst") or ""
    if len(est) >= 10 and est[4] == "-":
        return est[:10]
    try:
        return datetime.strptime(day["gameDate"][:10], "%m/%d/%Y").date().isoformat()
    except (KeyError, ValueError):
        return ""


def game_ids_on(schedule, date):
    target = date.isoformat()
    return [g["gameId"] for day in schedule["leagueSchedule"]["gameDates"]
            for g in day["games"] if game_date(day, g) == target]


def score_line(game):
    h, a = game["homeTeam"], game["awayTeam"]
    win_home = h["score"] > a["score"]
    away = f"**{a['teamTricode']} {a['score']}**" if not win_home else f"{a['teamTricode']} {a['score']}"
    home = f"**{h['teamTricode']} {h['score']}**" if win_home else f"{h['teamTricode']} {h['score']}"
    return f"{away} @ {home}"


def find_team_game(games, tri):
    for g in games:
        if tri in (g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]):
            return g
    return None


def duel_lines(games):
    played, out = [], []
    for p in DUEL:
        g = find_team_game(games, p["team"])
        if not g:
            out.append(f"**{p['name']}**: no game")
            continue
        team = g["homeTeam"] if g["homeTeam"]["teamTricode"] == p["team"] else g["awayTeam"]
        me = next((x for x in team.get("players", []) if x.get("personId") == p["id"]), None)
        if not me or me.get("played") != "1":
            out.append(f"**{p['name']}**: DNP")
        else:
            played.append((p, me["statistics"]["points"]))
    if not played:
        return []          # nobody played: skip the section

    played.sort(key=lambda x: -x[1])
    lines, rank = [], 0
    for i, (p, pts) in enumerate(played):
        if i and pts < played[i - 1][1]:
            rank = i       # ties share a medal
        lines.append(f"{MEDALS[rank]} **{p['name']}**: {pts} PTS")
    lines += out

    top = [p for p, pts in played if pts == played[0][1]]
    if len(played) == 1:
        verdict = f"🏆 {played[0][0]['short']} wins by default."
    elif len(top) > 1:
        verdict = f"🤝 Tie at the top: {' & '.join(p['short'] for p in top)} with {played[0][1]}."
    else:
        verdict = f"🏆 {played[0][0]['short']} wins by {played[0][1] - played[1][1]}."
    return lines + [verdict]


def roast_lines(games):
    losers = []
    for tri in ("NYK", "SAS"):
        g = find_team_game(games, tri)
        if not g:
            continue
        mine, opp = ((g["homeTeam"], g["awayTeam"]) if g["homeTeam"]["teamTricode"] == tri
                     else (g["awayTeam"], g["homeTeam"]))
        if mine["score"] < opp["score"]:
            losers.append(random.choice(ROASTS[tri]).format(
                winner=opp["teamTricode"], score=f"{opp['score']}–{mine['score']}"))
    if len(losers) == 2:
        losers.append(BOTH_LOST)
    return losers


def block(text, **kw):
    return {"type": "TextBlock", "text": text, "wrap": True, "spacing": "Small", **kw}


def build_card(date, games):
    body = [block(f"☕ NBA Morning Recap — {date.strftime('%a, %b %-d')}",
                  weight="Bolder", size="Large", spacing="None")]
    duel = duel_lines(games)
    if duel:
        body.append(block("🥊 Points Battle", weight="Bolder", spacing="Medium"))
        body += [block(l) for l in duel]
    roasts = roast_lines(games)
    if roasts:
        body.append(block("🔥 Roast Corner", weight="Bolder", color="Attention", spacing="Medium"))
        body += [block(l) for l in roasts]
    body.append(block("🏀 Last Night's Scores", weight="Bolder", spacing="Medium"))
    body += [block(score_line(g)) for g in games]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard", "version": "1.4", "body": body},
        }],
    }


def post(payload):
    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not url:
        print("DRY RUN — card text:")
        for b in payload["attachments"][0]["content"]["body"]:
            print("  " + b["text"])
        return
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Posted to Teams: HTTP {resp.status}")


def main():
    date = target_date()
    ids = game_ids_on(fetch_json(SCHEDULE_URL), date)
    games = []
    for gid in ids:
        box = fetch_or_none(BOXSCORE_URL.format(gid=gid))
        if box and box["game"].get("gameStatus") == 3:
            games.append(box["game"])
    if not games:
        print(f"No finished games on {date}. Nothing to post.")
        return
    post(build_card(date, games))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
