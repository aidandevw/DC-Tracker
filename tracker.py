#!/usr/bin/env python3
"""
Roblox Early-Game Spike Tracker
-------------------------------
Finds YOUNG Roblox games (created within N days), records their CCU over time,
and pings a Discord webhook when one suddenly spikes (a likely algorithm push).

Usage:
  python tracker.py                 # run forever (PC / VPS)
  python tracker.py --once          # run one cycle and exit (GitHub Actions / cron)
  python tracker.py --test-webhook  # send a test message to your webhook
"""
import argparse
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TRACKER_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.environ.get("TRACKER_STATE", os.path.join(HERE, "state.json"))
QUIET = os.environ.get("TRACKER_QUIET") == "1"  # hide game names in logs (public GitHub repos)

EXPLORE_URL = "https://apis.roblox.com/explore-api/v1/get-sorts"
SEARCH_URL = "https://apis.roblox.com/search-api/omni-search"
GAMES_URL = "https://games.roblox.com/v1/games"
VOTES_URL = "https://games.roblox.com/v1/games/votes"
ICONS_URL = "https://thumbnails.roblox.com/v1/games/icons"

DEFAULTS = {
    "webhook_url": "",                # fallback if no tier matches / no tier URL set
    "webhook_tiers": [                # route alerts by CCU (url can also come from env var)
        {"name": "low", "min": 50, "max": 499, "url": "", "env": "WEBHOOK_LOW"},
        {"name": "mid", "min": 500, "max": 999, "url": "", "env": "WEBHOOK_MID"},
        {"name": "popular", "min": 1000, "max": None, "url": "", "env": "WEBHOOK_POPULAR"},
    ],
    "mention": "",                    # e.g. "<@&ROLE_ID>" or "@here" to ping
    "max_game_age_days": 45,          # only games created within this many days
    "min_ccu": 150,                   # ignore spikes below this CCU
    "spike_multiplier": 2.0,          # CCU must be >= baseline * this
    "min_ccu_gain": 100,              # ...and gain at least this many players
    "lookback_minutes": 90,           # baseline window
    "new_game_alert_ccu": 1500,       # alert when a young game is first found already above this
    "alert_cooldown_hours": 6,
    "escalation_multiplier": 2.0,     # bypass cooldown if CCU doubles again since last alert
    "max_alerts_per_cycle": 10,
    "poll_interval_seconds": 300,
    "explore_pages": 6,
    "search_keywords": [],
    "search_pages": 1,
    "extra_universe_ids": [],
    "ignore_universe_ids": [],
    "ignore_name_keywords": [],
    "watch_min_ccu": 15,              # drop a watched game if its peak stays below this...
    "watch_grace_hours": 24,          # ...after being tracked this long
    "max_watchlist": 2500,
    "history_keep_hours": 48,
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
})


# ----------------------------------------------------------------- utilities
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def label(name, uid):
    return "a game (hidden in quiet mode)" if QUIET else f"{name} ({uid})"


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    env_hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env_hook:
        cfg["webhook_url"] = env_hook
    for tier in cfg.get("webhook_tiers") or []:
        env_val = os.environ.get(tier.get("env") or "", "").strip()
        if env_val:
            tier["url"] = env_val
    return cfg


def webhook_for(cfg, ccu):
    """Pick the webhook whose CCU range contains ccu; fall back to webhook_url."""
    for tier in cfg.get("webhook_tiers") or []:
        lo, hi = tier.get("min") or 0, tier.get("max")
        if ccu >= lo and (hi is None or ccu <= hi) and tier.get("url"):
            return tier["url"], tier.get("name", "?"), tier.get("mention") or cfg.get("mention", "")
    return cfg.get("webhook_url", ""), "default", cfg.get("mention", "")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
                s.setdefault("games", {})
                s.setdefault("old", {})
                return s
        except (ValueError, OSError):
            log("state.json was unreadable, starting fresh")
    return {"games": {}, "old": {}, "last_run": None}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def parse_time(s):
    """Parse Roblox ISO timestamps like 2026-11-01T12:34:56.123Z -> unix seconds."""
    if not s:
        return None
    try:
        base = s.replace("Z", "").split(".")[0].split("+")[0]
        return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def get_json(url, params=None, tries=4):
    for attempt in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep((2 ** attempt) * 3 + random.random())
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(2 ** attempt)
    return None


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def collect_universe_ids(obj, out):
    """Walk any JSON and grab every 'universeId' — survives Roblox reshaping responses."""
    if isinstance(obj, dict):
        uid = obj.get("universeId")
        if isinstance(uid, int) or (isinstance(uid, str) and uid.isdigit()):
            out.add(int(uid))
        for v in obj.values():
            collect_universe_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_universe_ids(v, out)


# ----------------------------------------------------------------- discovery
def discover_explore(cfg):
    ids, token, sid = set(), None, str(uuid.uuid4())
    for _ in range(int(cfg["explore_pages"])):
        params = {"sessionId": sid, "device": "computer", "country": "all"}
        if token:
            params["sortsPageToken"] = token
        data = get_json(EXPLORE_URL, params)
        if not data:
            break
        collect_universe_ids(data, ids)
        token = data.get("nextSortsPageToken") if isinstance(data, dict) else None
        if not token:
            break
        time.sleep(0.4)
    return ids


def discover_search(cfg):
    ids = set()
    for kw in cfg["search_keywords"]:
        sid, token = str(uuid.uuid4()), None
        for _ in range(int(cfg["search_pages"])):
            params = {"searchQuery": kw, "sessionId": sid, "pageType": "all"}
            if token:
                params["pageToken"] = token
            data = get_json(SEARCH_URL, params)
            if not data:
                break
            collect_universe_ids(data, ids)
            token = data.get("nextPageToken") if isinstance(data, dict) else None
            if not token:
                break
            time.sleep(0.4)
        time.sleep(0.4)
    return ids


# ----------------------------------------------------------------- game data
def fetch_details(uids):
    out = {}
    for batch in chunks(uids, 50):
        data = get_json(GAMES_URL, {"universeIds": ",".join(map(str, batch))})
        for g in (data or {}).get("data", []) or []:
            if "id" in g:
                out[int(g["id"])] = g
        time.sleep(0.35)
    return out


def fetch_votes(uids):
    out = {}
    for batch in chunks(uids, 50):
        data = get_json(VOTES_URL, {"universeIds": ",".join(map(str, batch))})
        for v in (data or {}).get("data", []) or []:
            out[int(v.get("id", 0))] = (v.get("upVotes", 0) or 0, v.get("downVotes", 0) or 0)
    return out


def fetch_icons(uids):
    out = {}
    for batch in chunks(uids, 50):
        data = get_json(ICONS_URL, {"universeIds": ",".join(map(str, batch)), "size": "150x150",
                                    "format": "Png", "isCircular": "false",
                                    "returnPolicy": "PlaceHolder"})
        for t in (data or {}).get("data", []) or []:
            if t.get("imageUrl"):
                out[int(t.get("targetId", 0))] = t["imageUrl"]
    return out


# ----------------------------------------------------------------- detection
def evaluate(g, now, cfg, first_seen_now):
    hist = g["history"]
    cur = hist[-1][1]
    if cur < cfg["min_ccu"]:
        return None

    lookback = cfg["lookback_minutes"] * 60
    window = [(t, c) for t, c in hist[:-1] if 0 < now - t <= lookback]

    alert = None
    if not window:
        if first_seen_now and cur >= cfg["new_game_alert_ccu"] and not g.get("last_alert_ts"):
            alert = {"kind": "new", "cur": cur}
    else:
        base_t, base = min(window, key=lambda x: x[1])
        ratio = cur / max(base, 1)
        gain = cur - base
        if ratio >= cfg["spike_multiplier"] and gain >= cfg["min_ccu_gain"]:
            alert = {"kind": "spike", "cur": cur, "base": base, "ratio": ratio,
                     "gain": gain, "mins": max(1, round((now - base_t) / 60))}
    if not alert:
        return None

    last_ts, last_ccu = g.get("last_alert_ts"), g.get("last_alert_ccu") or 0
    if last_ts and now - last_ts < cfg["alert_cooldown_hours"] * 3600:
        if last_ccu and cur >= last_ccu * cfg["escalation_multiplier"]:
            alert["kind"] = "escalation"
            alert["prev_alert_ccu"] = last_ccu
        else:
            return None
    alert["score"] = alert.get("ratio", 3.0) * (cur ** 0.5)
    return alert


# ----------------------------------------------------------------- discord
KIND_STYLE = {
    "spike": ("🚀 CCU SPIKE", 0xF39C12),
    "escalation": ("🔥 STILL CLIMBING", 0xE74C3C),
    "new": ("👀 NEW YOUNG GAME ALREADY HOT", 0x3498DB),
}


def fmt_age(days):
    return f"{days * 24:.0f} hours" if days < 2 else f"{days:.1f} days"


def build_embed(uid, info, alert, votes, icon):
    title_prefix, color = KIND_STYLE[alert["kind"]]
    up, down = votes.get(uid, (0, 0))
    like = f"{up / (up + down) * 100:.0f}% ({up + down:,} votes)" if up + down else "n/a"
    created = parse_time(info.get("created"))
    age_days = (time.time() - created) / 86400 if created else 0
    creator = (info.get("creator") or {}).get("name", "?")

    if alert["kind"] == "spike":
        desc = (f"CCU **{alert['base']:,} → {alert['cur']:,}** "
                f"(**{alert['ratio']:.1f}x**, +{alert['gain']:,} in ~{alert['mins']} min)")
    elif alert["kind"] == "escalation":
        desc = (f"Was **{alert['prev_alert_ccu']:,}** at last alert, now **{alert['cur']:,}** "
                f"({alert['cur'] / alert['prev_alert_ccu']:.1f}x)")
    else:
        desc = f"First time seen and already at **{alert['cur']:,}** CCU"

    embed = {
        "title": f"{title_prefix}: {info.get('name', 'Unknown')}"[:250],
        "url": f"https://www.roblox.com/games/{info.get('rootPlaceId')}",
        "description": desc,
        "color": color,
        "fields": [
            {"name": "CCU now", "value": f"{alert['cur']:,}", "inline": True},
            {"name": "Game age", "value": fmt_age(age_days), "inline": True},
            {"name": "Visits", "value": f"{info.get('visits', 0):,}", "inline": True},
            {"name": "Like ratio", "value": like, "inline": True},
            {"name": "Favorites", "value": f"{info.get('favoritedCount', 0):,}", "inline": True},
            {"name": "Creator", "value": str(creator)[:100], "inline": True},
        ],
        "footer": {"text": f"Universe {uid} • Place {info.get('rootPlaceId')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


def send_webhook(url, embeds, content=None):
    if not url:
        log("No webhook_url set — alert printed only.")
        for e in embeds:
            print(json.dumps(e, indent=2))
        return False
    payload = {"username": "Roblox Spike Tracker", "embeds": embeds,
               "allowed_mentions": {"parse": ["roles", "users", "everyone"]}}
    if content:
        payload["content"] = content
    for _ in range(4):
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as ex:
            log(f"Webhook error: {ex}")
            time.sleep(2)
            continue
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 300:
            log(f"Webhook failed: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    return False


# ----------------------------------------------------------------- main cycle
def run_cycle(cfg, state):
    now = time.time()
    warmup = state.get("last_run") is None
    games, old = state["games"], state["old"]
    ignore = set(map(int, cfg["ignore_universe_ids"]))
    bad_words = [w.lower() for w in cfg["ignore_name_keywords"]]

    discovered = discover_explore(cfg) | discover_search(cfg) | set(map(int, cfg["extra_universe_ids"]))
    to_fetch = {u for u in discovered if str(u) not in old} | set(map(int, games.keys()))
    to_fetch -= ignore
    log(f"Discovered {len(discovered)} games, checking {len(to_fetch)} "
        f"(watchlist {len(games)})")

    if not to_fetch:
        log("Nothing fetched — Roblox may be rate-limiting or blocking this IP.")
        return

    details = fetch_details(to_fetch)
    max_age = cfg["max_game_age_days"] * 86400
    keep_hist = cfg["history_keep_hours"] * 3600
    pending = []

    for uid, info in details.items():
        key = str(uid)
        created = parse_time(info.get("created"))
        name = info.get("name", "")
        if not created or now - created > max_age or any(w in name.lower() for w in bad_words):
            old[key] = int(now)
            games.pop(key, None)
            continue

        first_seen_now = key not in games
        g = games.setdefault(key, {"name": name, "first_seen": int(now), "history": []})
        g["name"] = name
        ccu = int(info.get("playing") or 0)
        g["history"].append([int(now), ccu])
        g["history"] = [h for h in g["history"] if now - h[0] <= keep_hist]
        g["peak"] = max(c for _, c in g["history"])

        alert = evaluate(g, now, cfg, first_seen_now)
        if alert:
            pending.append((uid, info, alert))

    # prune dead / oversized watchlist
    for key in list(games):
        g = games[key]
        if now - g["first_seen"] > cfg["watch_grace_hours"] * 3600 and g.get("peak", 0) < cfg["watch_min_ccu"]:
            games.pop(key)
    if len(games) > cfg["max_watchlist"]:
        ranked = sorted(games, key=lambda k: games[k].get("peak", 0), reverse=True)
        for key in ranked[cfg["max_watchlist"]:]:
            games.pop(key)
    if len(old) > 50000:  # keep the "known old" cache bounded
        for key in sorted(old, key=old.get)[:10000]:
            old.pop(key)

    young = sum(1 for _ in games)
    if warmup:
        log(f"Warm-up run: recorded baselines for {young} young games, no alerts sent.")
        return

    pending.sort(key=lambda p: p[2]["score"], reverse=True)
    pending = pending[:cfg["max_alerts_per_cycle"]]
    if not pending:
        log(f"No spikes. Tracking {young} young games.")
        return

    alert_ids = [p[0] for p in pending]
    votes, icons = fetch_votes(alert_ids), fetch_icons(alert_ids)
    for uid, info, alert in pending:
        embed = build_embed(uid, info, alert, votes, icons.get(uid))
        url, tier_name, mention = webhook_for(cfg, alert["cur"])
        embed["footer"]["text"] += f" • {tier_name} tier"
        ok = send_webhook(url, [embed], mention or None)
        g = games[str(uid)]
        g["last_alert_ts"], g["last_alert_ccu"] = int(now), alert["cur"]
        ccu_txt = "" if QUIET else f" @ {alert['cur']} CCU"
        log(f"ALERT {alert['kind']}: {label(info.get('name'), uid)}{ccu_txt}"
            f"{'' if ok else ' (webhook not sent)'}")
        time.sleep(1.2)


def main():
    ap = argparse.ArgumentParser(description="Roblox early-game CCU spike tracker")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--test-webhook", action="store_true", help="send a test Discord message")
    ap.add_argument("--duration", type=float, default=0,
                    help="keep looping for this many minutes, then exit (GitHub Actions)")
    args = ap.parse_args()
    cfg = load_config()

    if args.test_webhook:
        targets = [(t.get("name"), t.get("url"), t.get("mention") or cfg.get("mention", ""))
                   for t in cfg.get("webhook_tiers") or [] if t.get("url")]
        if cfg.get("webhook_url"):
            targets.append(("default", cfg["webhook_url"], cfg.get("mention", "")))
        if not targets:
            log("No webhooks configured.")
        for name, url, mention in targets:
            ok = send_webhook(url, [{"title": f"✅ Tracker webhook works ({name} tier)",
                                     "description": "You'll get spike alerts here.",
                                     "color": 0x2ECC71}], mention or None)
            log(f"{name}: {'test sent!' if ok else 'test FAILED — check the URL'}")
            time.sleep(1)
        return

    started = time.time()
    while True:
        cycle_start = time.time()
        state = load_state()
        try:
            run_cycle(cfg, state)
        except Exception as ex:  # keep the loop alive no matter what
            log(f"Cycle error: {ex!r}")
        state["last_run"] = int(time.time())
        save_state(state)
        if args.once:
            break
        wait = max(5, cfg["poll_interval_seconds"] - (time.time() - cycle_start))
        if args.duration and (time.time() - started) + wait > args.duration * 60:
            log("Duration reached, exiting so the next run can take over.")
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
