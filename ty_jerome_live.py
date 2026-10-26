"""
NBA Live Watcher

  * Ty Jerome bucket alerts, with headers that get louder as his night gets better
  * Grizzlies pre-game heads-up ~15 min before tip
  * Flagrant 2 alerts from ANY game
  * Shai Gilgeous-Alexander free-throw milestones (10th, 15th, 20th attempt)

Runs on an hourly schedule (see ty-jerome-live.yml). Each run watches every game that is
live or tipping off within LOOKAHEAD, and exits when there's nothing left to watch.
Progress is saved to state.json (committed back to the repo by the workflow) so a later
run picks up where the last one left off without repeating messages.

Env vars:
  TEAMS_WEBHOOK_URL  - Teams workflow webhook URL (blank = dry run, prints messages)
  GAME_ID            - optional: replay one specific game right now (test mode, no state saved)
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
from zoneinfo import ZoneInfo

# ---------------- settings ----------------
JEROME_ID = 1629660
SGA_ID = 1628983
TEAM = "MEM"

PTS_MILESTONES = [20, 30, 40, 50]
SGA_MILESTONES = [10, 15, 20]

POLL_SECONDS = 40
LOOKAHEAD = timedelta(hours=2)          # start watching when a game is this close to tipoff
PREGAME_LEAD = timedelta(minutes=15)    # heads-up this long before Grizzlies tip
STALE_AFTER = timedelta(hours=5)        # give up on a game this long after tip
MAX_RUNTIME = timedelta(hours=5, minutes=30)  # GitHub kills jobs at 6h; next run resumes
STATE_FILE = "state.json"
CT = ZoneInfo("America/Chicago")

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"
PBP_URL = "https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{gid}.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (ty-jerome-live script)",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
}

START = datetime.now(timezone.utc)

# ---------------- message copy ----------------
# Ty Jerome headers by points tier. {pts} {threes} {opp} are filled in.
TIER_HEADERS = [
    [  # 0-9
        "🐻 Ty Jerome scores",
        "Bucket: Ty Jerome",
        "Ty Jerome is on the board",
        "🐻 Jerome with the bucket",
    ],
    [  # 10-19
        "🔥 Ty Jerome is heating up",
        "Somebody check on {opp}, Ty's cooking",
        "📈 Ty Jerome stock: BUY",
        "Ty Jerome said 'my turn' ({pts} PTS)",
    ],
    [  # 20-29
        "🚨🚨 TY JEROME IS HIM 🚨🚨",
        "{opp} HAS NO ANSWER FOR TY JEROME",
        "This is a Ty Jerome appreciation chat now ({pts} PTS)",
        "🚨 TY JEROME IS PLAYING LIKE IT'S A CONTRACT YEAR 🚨",
    ],
    [  # 30-39
        "🚨🔥🚨 TY JEROME HAS {pts}. CALL YOUR FAMILY 🚨🔥🚨",
        "🔥 TY JEROME IS COOKING {opp} IN THEIR OWN KITCHEN 🔥",
        "SOMEBODY GET {opp} A TIMEOUT. TWO TIMEOUTS. ({pts} PTS)",
        "🏛️ PUT TY JEROME IN THE HALL OF FAME. TONIGHT. {pts} POINTS.",
    ],
    [  # 40+
        "🐐🐐🐐 TY JEROME {pts} POINTS. THIS IS NOT A DRILL 🐐🐐🐐",
        "🚨🐐🚨 TY JEROME IS HIM. {pts} POINTS. HIM. 🚨🐐🚨",
        "WILT WHO??? TY JEROME HAS {pts} 🐐",
        "🚔 THE NBA SHOULD INVESTIGATE TY JEROME. {pts} POINTS. 🚔",
    ],
]

FT_HEADERS = [  # free throws while he's under 20
    "Ty Jerome at the line. Cash. 💵",
    "Free throw, Ty Jerome. They all count.",
    "Ty Jerome from the charity stripe 🎗️",
]

THREE_HEADERS = {  # used when the bucket is a three and he has at least 3
    3: "💦 That's {threes} threes for Ty Jerome",
    4: "💦💦 TY JEROME FROM DOWNTOWN AGAIN ({threes} THREES)",
    5: "🌊🌊🌊 SPLASH #{threes}. TY JEROME HAS LOST HIS MIND FROM THREE",
    7: "🌊🌊🌊🌊 {threes} THREES. STEPH CURRY IS TAKING NOTES 📝",
}

MILESTONE_HEADERS = {
    20: "🚨 TY JEROME HITS 20 🚨",
    30: "🚨🚨🚨 30 BALL FOR TY JEROME 🚨🚨🚨",
    40: "🔥🔥🔥🔥 FORTY. FOUR-ZERO. TY JEROME. 🔥🔥🔥🔥",
    50: "🐐🐐🐐🐐🐐 FIFTY. TY JEROME HAS FIFTY. 🐐🐐🐐🐐🐐",
}

PREGAME_HEADERS = [
    "🚨 TY JEROME ALERTS ARE LIVE 🚨",
    "📣 Attention: Ty Jerome is clocking in",
    "🐻 Ty Jerome Watch is now in session",
]
PREGAME_KICKERS = [
    "Phones on loud.",
    "Clear your schedule.",
    "{opp} has been warned.",
    "Hydrate. It's going to be a long night of notifications.",
]

SGA_LINES = {
    10: [
        "🎣 Free throw #{n}. Shai jumped into a defender, threw his head back, and the refs bit. Again.",
        "🎭 Shai's at the line for the {n}th time. The flop was a 6/10 but the refs gave it a 10.",
        "🛟 Free throw #{n}. Shai got stuck in traffic, so the refs sent a tow truck. Another bailout.",
        "📞 Shai picked up his dribble, panicked, and called the refs. They answered. Free throw #{n}.",
        "🎟️ Free throw #{n}. Shai has a reserved seat at the line and the refs are holding the door.",
    ],
    15: [
        "🧾 FIFTEEN free throws. Shai hasn't beaten his man off the dribble since the first quarter, but the refs have been great.",
        "🎣 #15. Shai's arm-hook rip-through is the most reliable play in basketball and the refs are its biggest fans.",
    ],
    20: [
        "🏆 TWENTY free throws. The refs should get a share of Shai's points tonight.",
        "🎭 Free throw #20. Shai has flailed more times than he's dribbled, and the refs bought every single one.",
    ],
}

FLAGRANT_HEADERS = [
    "🟥 FLAGRANT 2: {player} has been sent to the locker room to think about what he did",
    "🚪 FLAGRANT 2. Bye bye, {player}. 👋",
    "🟥 EJECTED: {player} picked violence and the refs picked the exit",
]


# ---------------- helpers ----------------

def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_or_none(url):
    try:
        return fetch_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        if not (isinstance(e, urllib.error.HTTPError) and e.code in (403, 404)):
            log(f"fetch failed ({e}) for {url}")
        return None


def parse_utc(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_clock(iso_clock):
    m = re.match(r"PT(\d+)M([\d.]+)S", iso_clock or "")
    return f"{int(m.group(1))}:{int(float(m.group(2))):02d}" if m else ""


def fmt_period(p):
    p = p or 0
    return f"Q{p}" if p <= 4 else ("OT" if p == 5 else f"{p - 4}OT")


def when(a):
    return f"{fmt_period(a.get('period'))} {fmt_clock(a.get('clock'))}".strip()


def mem_score(a, g):
    """'MEM 54–49 GSW' from the Grizzlies' point of view."""
    h, aw = a.get("scoreHome", "?"), a.get("scoreAway", "?")
    if g["home"] == TEAM:
        return f"MEM {h}–{aw} {g['away']}"
    return f"MEM {aw}–{h} {g['home']}"


def game_score(a, g):
    return f"{g['away']} {a.get('scoreAway', '?')} @ {g['home']} {a.get('scoreHome', '?')}"


def opp_of(g):
    return g["away"] if g["home"] == TEAM else g["home"]


# ---------------- posting ----------------

def card(title, lines, size="Medium", color="Default"):
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": size,
             "color": color, "wrap": True}]
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


def post(title, lines, size="Medium", color="Default"):
    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not url:
        log(f"DRY RUN → [{size}/{color}] {title} | {' / '.join(lines)}")
        return
    data = json.dumps(card(title, lines, size, color)).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                log(f"posted '{title}' (HTTP {resp.status})")
                return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            log(f"post failed ({e}), retry {attempt + 1}/3")
            time.sleep(5)


# ---------------- Ty Jerome ----------------

def tier_of(pts):
    return 0 if pts < 10 else 1 if pts < 20 else 2 if pts < 30 else 3 if pts < 40 else 4


TIER_STYLE = [("Medium", "Default"), ("Medium", "Warning"), ("Large", "Attention"),
              ("ExtraLarge", "Attention"), ("ExtraLarge", "Attention")]


def jerome_events(actions):
    """Every Jerome made shot / FT in order, with his running totals after it."""
    pts = threes = 0
    out = []
    for a in actions:
        if a.get("personId") != JEROME_ID or a.get("shotResult") != "Made":
            continue
        kind = a.get("actionType", "")
        desc = a.get("description", "")
        is3 = kind == "3pt" or "3PT" in desc
        is_ft = kind == "freethrow"
        before = pts
        m = re.search(r"\((\d+) PTS\)", desc)
        pts = int(m.group(1)) if m else pts + (3 if is3 else 1 if is_ft else 2)
        if is3:
            threes += 1
        out.append({"a": a, "before": before, "pts": pts, "threes": threes, "is3": is3, "is_ft": is_ft})
    return out


def jerome_header(ev, opp):
    fill = {"pts": ev["pts"], "threes": ev["threes"], "opp": opp}
    style = TIER_STYLE[tier_of(ev["pts"])]
    for m in reversed(PTS_MILESTONES):
        if ev["before"] < m <= ev["pts"]:
            return MILESTONE_HEADERS[m].format(**fill), ("ExtraLarge", "Attention")
    if ev["is3"] and ev["threes"] >= 3:
        key = max(k for k in THREE_HEADERS if k <= ev["threes"])
        return THREE_HEADERS[key].format(**fill), style
    tier = tier_of(ev["pts"])
    if ev["is_ft"] and tier <= 1:
        return random.choice(FT_HEADERS).format(**fill), style
    return random.choice(TIER_HEADERS[tier]).format(**fill), style


def jerome_messages(actions, last, first_pass, g):
    opp = opp_of(g)
    new = [e for e in jerome_events(actions) if e["a"]["actionNumber"] > last]
    if not new:
        return []
    if first_pass and len(new) > 1:
        top = new[-1]
        size, color = TIER_STYLE[tier_of(top["pts"])]
        lines = [f"{e['a'].get('description', '').strip()} — {when(e['a'])}" for e in new]
        return [(f"⏪ Catching up: Ty Jerome already has {top['pts']} PTS ({len(new)} buckets)",
                 lines, size, color)]
    msgs = []
    for e in new:
        title, (size, color) = jerome_header(e, opp)
        a = e["a"]
        lines = [f"{a.get('description', '').strip()} — {when(a)}",
                 f"{mem_score(a, g)} · Ty: {e['pts']} PTS, {e['threes']} 3PM"]
        msgs.append((title, lines, size, color))
    return msgs


def jerome_final(box, g):
    game = box["game"]
    mine, opp = ((game["homeTeam"], game["awayTeam"]) if game["homeTeam"]["teamTricode"] == TEAM
                 else (game["awayTeam"], game["homeTeam"]))
    result = "W" if mine["score"] > opp["score"] else "L"
    score = f"MEM {mine['score']}–{opp['score']} {result} vs {opp['teamTricode']}"
    me = next((p for p in mine.get("players", []) if p.get("personId") == JEROME_ID), None)
    if not me or me.get("played") != "1":
        return ("🏁 Final — no Ty Jerome tonight", [score], "Medium", "Default")
    s = me.get("statistics", {})
    pts = s.get("points", 0)
    stat = f"{pts} PTS · {s.get('threePointersMade', 0)} 3PM · {s.get('assists', 0)} AST"
    if pts >= 40:
        title = f"🏁 FINAL: TY JEROME. {pts}. POINTS. GOODNIGHT. 🐐"
    elif pts >= 30:
        title = f"🏁 FINAL: TY JEROME DROPPED {pts}. FRAME THIS. 🔥🔥🔥"
    elif pts >= 20:
        title = f"🏁 FINAL: TY JEROME DROPPED {pts} 🔥"
    elif pts >= 10:
        title = f"🏁 Final: Ty Jerome finished with {pts} PTS"
    elif pts > 0:
        title = f"🏁 Final: Ty Jerome finished with {pts}. We move."
    else:
        title = "🏁 Final: Ty Jerome finished with 0. We don't talk about it."
    size, color = TIER_STYLE[tier_of(pts)]
    return (title, [score, stat], size, color)


# ---------------- other alerts ----------------

def is_flagrant2(a):
    text = " ".join(str(a.get(k, "")) for k in ("actionType", "subType", "descriptor", "description")).lower()
    return "flagrant" in text and re.search(r"(type[-_ ]?2|flagrant[-_ ]?2)", text) is not None


def flagrant_messages(actions, last, gs, g):
    msgs = []
    done = gs.setdefault("f2", [])
    for a in actions:
        if a.get("actionNumber", 0) <= last or not is_flagrant2(a):
            continue
        who = a.get("personId") or a.get("description")
        if who in done:          # foul + ejection rows for the same player → one alert
            continue
        done.append(who)
        player = a.get("playerNameI") or a.get("playerName") or "Somebody"
        team = a.get("teamTricode", "")
        title = random.choice(FLAGRANT_HEADERS).format(player=f"{player} ({team})" if team else player)
        msgs.append((title, [a.get("description", "").strip(), f"{game_score(a, g)} — {when(a)}"],
                     "Large", "Attention"))
    return msgs


def sga_messages(actions, gs, g):
    fts = [a for a in actions if a.get("personId") == SGA_ID and a.get("actionType") == "freethrow"]
    sent = gs.setdefault("sga", [])
    hit = [m for m in SGA_MILESTONES if len(fts) >= m and m not in sent]
    if not hit:
        return []
    sent.extend(hit)
    m = max(hit)
    a = fts[m - 1]
    line = random.choice(SGA_LINES[m]).format(n=m)
    title = f"🚨 SHAI FREE THROW WATCH: #{m} 🚨"
    return [(title, [line, f"{game_score(a, g)} — {when(a)}"], "Large", "Warning")]


def maybe_pregame(g, gs, now):
    if gs.get("pregame") or g["tip"] - now > PREGAME_LEAD:
        return
    gs["pregame"] = True
    mins = int((g["tip"] - now).total_seconds() // 60)
    if mins < -20:
        return  # we're way late; skip the heads-up
    opp = opp_of(g)
    where = "vs" if g["home"] == TEAM else "@"
    tip_ct = g["tip"].astimezone(CT).strftime("%-I:%M %p")
    timing = f"tips off in {mins} min ({tip_ct} CT)" if mins > 0 else "is tipping off now"
    kicker = random.choice(PREGAME_KICKERS).format(opp=opp)
    post(random.choice(PREGAME_HEADERS), [f"Grizzlies {where} {opp} {timing}.", kicker], "Large", "Accent")


# ---------------- game loop ----------------

def check_game(g, gs):
    pbp = fetch_or_none(PBP_URL.format(gid=g["id"]))
    box = fetch_or_none(BOXSCORE_URL.format(gid=g["id"]))
    if pbp:
        actions = sorted(pbp["game"].get("actions", []), key=lambda a: a.get("actionNumber", 0))
        first_pass = "last" not in gs
        last = gs.get("last", 0)
        msgs = []
        if TEAM in (g["home"], g["away"]):
            msgs += jerome_messages(actions, last, first_pass, g)
        msgs += flagrant_messages(actions, last, gs, g)
        msgs += sga_messages(actions, gs, g)
        for m in msgs:
            post(*m)
        gs["last"] = max([last] + [a.get("actionNumber", 0) for a in actions])
    if box and box["game"].get("gameStatus") == 3:
        if TEAM in (g["home"], g["away"]):
            post(*jerome_final(box, g))
        gs["final"] = True
        log(f"{g['away']} @ {g['home']} is final")


def load_games(now):
    sched = fetch_json(SCHEDULE_URL)
    games = []
    for day in sched["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            try:
                tip = parse_utc(g["gameDateTimeUTC"])
            except (KeyError, ValueError):
                continue
            if now - STALE_AFTER <= tip <= now + LOOKAHEAD + MAX_RUNTIME:
                games.append({"id": g["gameId"], "tip": tip,
                              "home": g["homeTeam"].get("teamTricode") or "TBD",
                              "away": g["awayTeam"].get("teamTricode") or "TBD"})
    return games


def run(games, state, test=False):
    deadline = START + MAX_RUNTIME
    all_gs = state.setdefault("games", {})
    while datetime.now(timezone.utc) < deadline:
        now = datetime.now(timezone.utc)
        todo = [g for g in games if not all_gs.get(g["id"], {}).get("final")
                and (test or now - STALE_AFTER <= g["tip"] <= now + LOOKAHEAD)]
        if not todo:
            log("nothing live or coming up soon — done for this run")
            return
        for g in todo:
            gs = all_gs.setdefault(g["id"], {"tip": g["tip"].isoformat()})
            if not test and TEAM in (g["home"], g["away"]):
                maybe_pregame(g, gs, now)
            if test or g["tip"] - timedelta(minutes=1) <= now:
                check_game(g, gs)
        if not test:
            save_state(state)
        time.sleep(POLL_SECONDS)
    log("hit max runtime; the next scheduled run will pick up from saved state")


# ---------------- state ----------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"games": {}}


def save_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    state["games"] = {k: v for k, v in state.get("games", {}).items()
                      if "tip" not in v or datetime.fromisoformat(v["tip"]) > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def main():
    forced = os.environ.get("GAME_ID", "").strip()
    if forced:
        game = fetch_json(BOXSCORE_URL.format(gid=forced))["game"]
        tip = parse_utc(game["gameTimeUTC"]) if game.get("gameTimeUTC") else START
        g = {"id": forced, "tip": tip,
             "home": game["homeTeam"]["teamTricode"], "away": game["awayTeam"]["teamTricode"]}
        log(f"TEST MODE: replaying {g['away']} @ {g['home']} ({forced}); state is not saved")
        run([g], {"games": {}}, test=True)
        return

    state = load_state()
    games = load_games(START)
    log(f"{len(games)} games in range; watching any within {LOOKAHEAD} of tipoff")
    try:
        run(games, state)
    finally:
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error: {e}")
        sys.exit(1)
