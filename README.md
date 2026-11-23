# Roblox Early-Game Spike Tracker

Finds **young** Roblox games (default: created in the last 45 days), records their CCU every few minutes, and sends a **Discord webhook** alert when one suddenly jumps, which usually means the algorithm has started pushing it.

## How it works

1. **Discover:** every cycle it pulls the home/explore sorts and search results for your keywords.
2. **Filter:** it drops any game created more than `max_game_age_days` ago and remembers it so it never checks that game again.
3. **Watch:** every young game it finds goes on a watchlist. Each cycle it checks that game's CCU, even after the game drops off the front page.
4. **Alert** in three cases:
   - 🚀 **CCU SPIKE:** CCU is at least `spike_multiplier`× the lowest reading in the last `lookback_minutes`, it gained at least `min_ccu_gain` players, and it's above `min_ccu`.
   - 🔥 **STILL CLIMBING:** the game already alerted, and its CCU has doubled again (this skips the cooldown).
   - 👀 **NEW YOUNG GAME ALREADY HOT:** the tracker sees a young game for the first time, and it's already above `new_game_alert_ccu`.
5. The **first run is a silent warm-up.** It only records baselines, so you don't get spammed.

Each alert shows the CCU change, the game's age, visits, like ratio, favorites, creator, the icon, and a link to the game.

---

## Step 1: Make a Discord webhook (2 min)

1. In your Discord server, make a channel such as `#spike-alerts`.
2. Open the channel settings (⚙️), go to **Integrations → Webhooks → New Webhook**, and click **Copy Webhook URL**.
3. Keep the URL private. Anyone who has it can post in that channel.

---

## Option A: Run it on your PC (easiest, fully free)

Best data quality, because it checks every 5 minutes. The catch: it only runs while your PC is on.

1. Install **Python 3.10+** from python.org. On Windows, tick **"Add Python to PATH"** during install.
2. Put this folder somewhere, for example `C:\roblox-spike-tracker`.
3. Open `config.json` and paste your webhook into `"webhook_url": ""`.
4. Open a terminal in the folder and run:
   ```
   pip install -r requirements.txt
   python tracker.py --test-webhook
   ```
   A green "✅ Tracker webhook works" message should show up in Discord.
5. Start the tracker:
   ```
   python tracker.py
   ```
   Leave the window open. To make Windows start it automatically, use **Task Scheduler → Create Basic Task → "When I log on"**, with program `python`, arguments `tracker.py`, and "Start in" set to the folder.

---

## Option B: GitHub Actions (free, no PC needed)

GitHub runs the script on a schedule in the cloud, and the tracker's memory (`state.json`) is kept in the Actions cache between runs.

1. Make a free account on github.com and create a **New repository**.
   - **Public repo:** unlimited free Actions minutes. Logs only show counts. Game names, IDs and CCU are hidden because `TRACKER_QUIET` is on. Your code and `config.json` are visible to others, though.
   - **Private repo:** 2,000 free minutes per month, so edit the workflow cron to `*/30 * * * *` (every 30 minutes).
2. Upload **all** the files, keeping the `.github/workflows/tracker.yml` folder structure. On the repo page, use **Add file → Upload files** and drag the whole folder in.
   - **Do NOT** put your webhook in `config.json` here. Leave it as `""`.
3. Go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the webhook URL as the value.
4. Go to the **Actions** tab and enable workflows if GitHub asks. Open **Roblox Spike Tracker** and click **Run workflow**. The first run is the silent warm-up.
5. After that it runs by itself about every 10 minutes. Click any run to see its logs.

Things to know about GitHub Actions:
- Scheduled runs often start 5–20 minutes late when GitHub is busy. That's normal, and the 90-minute lookback window absorbs it.
- If a public repo has no commits for 60 days, GitHub turns off the schedule. Push any small change, or click "Enable workflow", to turn it back on.
- If the logs keep saying `Nothing fetched`, Roblox is probably blocking GitHub's servers. Switch to Option A or C.

## Option C: A free 24/7 cloud server

Oracle Cloud's "Always Free" VM, or any cheap VPS, gives you 5-minute checks around the clock:
```
sudo apt install -y python3-pip git
git clone <your repo> && cd roblox-spike-tracker
pip3 install -r requirements.txt
nohup python3 tracker.py > tracker.log 2>&1 &
```

---

## Tuning (`config.json`)

| Setting | Default | What it does |
|---|---|---|
| `max_game_age_days` | 45 | Only games created this recently count as young |
| `min_ccu` | 150 | Ignore spikes below this CCU |
| `spike_multiplier` | 2.0 | Required jump vs. the baseline (2.0 = doubled) |
| `min_ccu_gain` | 100 | Minimum number of new players in the spike |
| `lookback_minutes` | 90 | Window the baseline is taken from |
| `new_game_alert_ccu` | 1500 | Alert when a newly found young game is already this big |
| `alert_cooldown_hours` | 6 | Hours before the same game can alert again (unless it doubles again) |
| `search_keywords` | genres | Extra discovery through Roblox search. Add your niches here |
| `extra_universe_ids` | [] | Specific universe IDs to always watch |
| `ignore_universe_ids` / `ignore_name_keywords` | [] | Hide games you don't care about |
| `mention` | "" | `"@here"` or `"<@&ROLE_ID>"` to ping people on alerts |
| `poll_interval_seconds` | 300 | Loop speed for Option A/C. Don't go below about 120, or Roblox may rate-limit you |

**Presets**
- *Catch it super early (noisier):* `min_ccu: 50`, `min_ccu_gain: 40`, `spike_multiplier: 1.8`
- *Only the real breakouts:* `min_ccu: 500`, `min_ccu_gain: 400`, `spike_multiplier: 2.5`

**Finding a universe ID:** open
`https://apis.roblox.com/universes/v1/places/<PLACE_ID>/universe`
in your browser. The place ID is the number in the game's URL.

## Limitations
- The tracker can only find games that show up in explore sorts or search. A game nobody can see yet is invisible to it. More keywords means wider coverage.
- "Age" is when the universe was created. A dev who republishes into an old universe will look old.
- The Explore and Search APIs are undocumented and can change. The tracker grabs any `universeId` it finds in a response, so small format changes won't break it.
- CCU swings with time of day. Lower thresholds will sometimes catch peak-hour bumps rather than algorithm pushes.
