---
name: daily-briefing
description: Assemble a short personal morning briefing — date, local weather, and a clustered news summary from the user's own RSS feeds. Use when they ask for a "briefing", "morning update", "what's up today", or when a scheduled routine requests one.
---

# Daily Briefing

A tight, phone-readable morning briefing.

> Template skill. Tune the topics, clusters, and language to the user's preferences. The data
> sources (feeds, weather location, who it's addressed to) are configured in `~/ogma/.env`
> (`OGMA_RSS_FEEDS`, `OGMA_WEATHER_LOC`, `OGMA_OWNER_NAME`).

## How it runs (scheduled)
The `ogma-briefing.timer` (07:00 daily) runs `~/ogma/bin/briefing`, which gathers the data
*deterministically* and hands it to Claude to write:
- **News** comes from `~/ogma/bin/news-fetch` — it pulls the configured RSS feeds for items
  published **since the last briefing** (tracked in `~/ogma/state/last-briefing`). This avoids
  stale web-search results.
- **Weather** is fetched from wttr.in and passed in.
The script gives Claude these as text; Claude only summarises/clusters. To preview without sending:
`BRIEFING_DRYRUN=1 ~/ogma/bin/briefing`.

## Writing the briefing
1. Header: one line with today's date + the supplied weather line.
2. News — **synthesise, don't list every headline**:
   - Lead with stories that **recur across multiple sources** — that overlap is the signal for
     what matters today.
   - Add a few genuinely **outstanding** single items (big/surprising, or in the user's wheelhouse).
   - **Cluster by broad topic** with short headers; drop any cluster with nothing worth saying.
   - 1–2 sentences per item, in your own words; aggregate related items.
   - Work only from the provided feed items — never web-search or invent. If it's not in the list,
     it didn't happen.
3. Optionally one closing line on anything time-sensitive from memory (`project` notes), if relevant.

## Style
- No preamble ("Here is your briefing"). Just give it.
- Under ~20 lines.
- Reply in the user's language. If the feeds are multilingual, you may match each item to its
  source language — set the convention to the user's taste.
- If asked interactively (not via the script) and you lack the feed digest, you may run
  `~/ogma/bin/news-fetch` yourself for the same data.
