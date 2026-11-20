# Restock bot

Runs on GitHub Actions about every 5 minutes, so it works even when your computer is off. It sends a phone
alert through **ntfy** when:

- a product in `watchlist.json` goes from **sold out → in stock**, or a single size comes back, or
- a **new product** appears anywhere on the site.

Each alert shows the photo, price and sizes in stock. Tap it to open the product.
The bot uses only the Python standard library, so there's nothing to install.

## Add products

Edit `watchlist.json` (on github.com: open the file → pencil icon → Commit changes). Paste the product page URL.
Add `options` if you only care about one size or color (use the names shown on the site):

```json
{ "url": "https://www.chromehearts.com/socks/sport/176354XXXXXXXXX.html",
  "options": { "color": "Black", "size": "M" } }
```

If an option name is wrong, the run's log lists the valid choices.

## New products

Each run checks every shop category (found from the site's menu) and alerts you about any product it hasn't seen
before. To check only some categories, set `"categories": ["scents", "socks"]`. To turn this off, set
`"watch_new_products": false`. If more than 5 new products appear at once, you get 5 alerts plus one summary.

## Alerts

- **ntfy** (phone): install the free ntfy app and subscribe to your topic. The topic name is stored as the
  `NTFY_TOPIC` repository secret (Settings → Secrets and variables → Actions), so it isn't visible in this public repo.

**Test alert:** Actions tab → Restock check → Run workflow → tick "Send a test alert" → Run.

## How it remembers things

`state.json` records which products and sizes were in stock and every product seen so far. The workflow commits it
back to the repo whenever it changes. Alerts only fire on changes, so the first run never sends alerts.

## Running on a Mac instead

`python3 restock_bot.py` checks once. Private settings go in `settings.local.json`, which is never uploaded:

```json
{ "ntfy_topic": "your-topic", "imessage_to": ["+61400000000"] }
```

On a Mac it also shows a Mac notification, and can send iMessages to the numbers listed.

## Notes

- The site blocks rapid requests, so the bot waits 6 seconds between requests. When it's blocked, it skips that
  round and tries again next time. Keep the watchlist to about 20 items or fewer.
- GitHub turns off scheduled jobs in a repo that has no activity for 60 days. If alerts stop, open the Actions
  tab and re-enable the workflow.
