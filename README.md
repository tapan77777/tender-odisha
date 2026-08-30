# Tender Odisha Watcher

Checks https://tendersodisha.gov.in every 15 min. For anything **civil
construction / roads / buildings** related:
- fetches the tender's attached document
- extracts its text (reads the PDF directly, or OCRs it if it's a scan)
- summarizes it with Claude (scope, value, EMD, eligibility, verdict)
- saves the full record + the PDF itself to a dashboard
- pings your Telegram with the short version

Everything non-construction is tracked (so it's never re-checked) but
never fetched, summarized, or sent — this is what keeps it cheap and
keeps your Telegram from getting noisy.

## Setup

**1. Create a Telegram bot**
- Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the
  prompts → copy the bot token (`123456789:ABCdefGhIJKlmNoPQRstuVWxyz`).

**2. Get your chat ID**
- Send any message to your new bot first (it can't message you until you do).
- Open this in a browser with your token swapped in:
  `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
- Find `"chat":{"id":123456789, ...}` — that number is your chat ID.

**3. Get an Anthropic API key**
- Go to [console.anthropic.com](https://console.anthropic.com) → API Keys →
  Create Key. This is what pays for the summarization step — expect roughly
  half a cent per tender processed (Haiku 4.5 pricing), so a few dozen
  tenders a month costs cents, not dollars.

**4. Create a GitHub repo** and push everything in this folder —
`tender_watcher.py`, `requirements.txt`, `state.json`, `README.md`,
`.github/workflows/tender-watch.yml`, and the whole `docs/` folder
(this is important — the dashboard lives there).

**5. Add three repo secrets**
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY`

**6. Allow the workflow to push commits**
Repo → Settings → Actions → General → Workflow permissions → set to
"Read and write permissions". (It needs this to save updated tender
data and state back to the repo after each run.)

**7. Turn on the dashboard**
Repo → Settings → Pages → Source: "Deploy from a branch" → Branch:
`main`, folder: `/docs` → Save. GitHub gives you a URL like:
`https://<your-username>.github.io/<repo-name>/`
That's your dashboard link — share it with your client whenever you're ready.

**8. Trigger it once manually**
Repo → Actions tab → "Tender Watcher" → Run workflow. The **first run
seeds the baseline and won't send a Telegram alert**, but it *will*
populate the dashboard with whatever construction-related tenders are
currently listed — so you'll have something to look at immediately.

**9. Check it worked**
- Actions tab → open the finished run → "Run watcher" step. Look for a
  line like `Run summary: X new item(s) seen, Y construction-related`.
- Open your dashboard URL — you should see rows if Y was above 0.
- If a construction-related tender didn't get a PDF ("No PDF" badge on
  the dashboard), that's the document-fetch step not finding a download
  link on that particular tender's page — see the limitation below.

From here it runs on its own, every 15 minutes, no further action needed.

## Known limitations (read before relying on this)

- **Document fetching is the least-tested part.** I built this without
  being able to reach the live site from my own environment, so the
  logic that opens a tender's detail page and finds its document link
  is best-effort — it looks for links containing words like "download"
  or "attach" or ending in `.pdf`. If the portal's actual markup
  doesn't match that pattern, tenders will still get summarized (from
  the listing title alone, marked "low confidence") but without a PDF.
  Send me the Action log after a real run and I'll tighten this up.
- **Only the homepage's "latest 10" lists are watched.** If a
  department batch-posts more than 10 tenders in one 15-minute window,
  the oldest in that batch could scroll off before a run catches it.
- **The construction keyword list is a blunt instrument.** It's the
  `CONSTRUCTION_KEYWORDS` list near the top of `tender_watcher.py` —
  edit it directly to add/remove terms. It's tuned to over-match on
  purpose (a false positive costs a few cents; a false negative means
  a real tender never reaches you at all).
- **OCR is capped at 6 pages per document** to keep run time and cost
  reasonable — fine for typical NIT notices, but a long scanned
  document will only get its first 6 pages read.
- **GitHub's 15-min schedule isn't exact** and can drift under load;
  and scheduled workflows auto-pause after 60 days with no repo
  activity (a manual "Run workflow" click resets that clock).
- **Public dashboard, unauthenticated.** Anyone with the link can view
  it. Fine for this data (it's all public tender info anyway), but
  worth knowing.
