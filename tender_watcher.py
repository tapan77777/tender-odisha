"""
Tender Odisha Watcher
----------------------
Polls the Government of Odisha eProcurement homepage every run, finds
new tenders/corrigendums, and for anything construction-related:
  1. fetches the tender's attached document
  2. extracts its text (PDF text layer, or OCR if it's a scan)
  3. summarizes it with Claude (scope, value, EMD, eligibility, verdict)
  4. saves the record + PDF into docs/data/ for the dashboard
  5. pings Telegram with the short version

Everything non-construction still gets tracked (so it's never re-checked)
but is never fetched, summarized, or sent -- keeping cost and noise down.

Run it manually:
    TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx ANTHROPIC_API_KEY=xxx \
        python tender_watcher.py
"""

import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

HOME_URL = "https://tendersodisha.gov.in/nicgep/app?page=Home&service=page"

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
INDEX_FILE = DATA_DIR / "index.json"

MAX_KEYS_KEPT = 500       # bound state.json size
MAX_INDEX_RECORDS = 500   # bound the dashboard's index.json size
MAX_DOC_CHARS = 12000     # cap text sent to Claude per document
MAX_OCR_PAGES = 6         # cap pages OCR'd for scanned documents
TELEGRAM_CHUNK = 3800
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Keywords used to decide "is this civil-construction-related". Tune this
# list freely -- it's the whole filter. Err on the side of over-matching;
# a false positive just costs a few cents in wasted summarization, a false
# negative means the tender never reaches the client at all.
CONSTRUCTION_KEYWORDS = [
    "road", "bridge", "culvert", "building", "construction", "rcc",
    "pwd", "embankment", "drainage", "check dam", "dam ", "canal",
    "irrigation", "widening", "black topping", "blacktopping", "asphalt",
    "highway", "flyover", "housing", "repair", "renovation",
    "improvement", "upgradation", "strengthening", "retaining wall",
    "boundary wall", "compound wall", "wtp", "water supply", "sewerage",
    "civil work", "earth work", "footpath", "flooring", "structure",
]


# ---------------------------------------------------------------------------
# Fetch + parse the listing page
# ---------------------------------------------------------------------------

def fetch_html(session: requests.Session) -> str:
    resp = session.get(HOME_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_section(soup: BeautifulSoup, header_label: str) -> list[dict]:
    """
    Locate the listing table by its header row -- a <tr> whose four cells
    read exactly "<header_label>", "Reference No", "Closing Date",
    "Bid Opening Date" -- then read the sibling 4-cell rows below it. The
    homepage nests many tables inside one another, so matching the header
    row (rather than the first table that merely *contains* those words)
    is what keeps this from scraping the page chrome.
    """
    header_row = None
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) == 4 and cells[0].lower() == header_label.lower():
            header_row = tr
            break
    if header_row is None:
        return []

    table = header_row.find_parent("table")
    items, seen = [], set()
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 4:
            continue
        link = cells[0].find("a")
        raw_title = cells[0].get_text(" ", strip=True)
        # drop the header row and the "1. " / "2. " ordinal prefix
        if raw_title.lower() == header_label.lower():
            continue
        title = re.sub(r"^\d+\.\s*", "", raw_title)
        if not title:
            continue
        href = link.get("href", "") if link else ""
        ref_no = cells[1].get_text(" ", strip=True)
        closing_date = cells[2].get_text(" ", strip=True)
        opening_date = cells[3].get_text(" ", strip=True)
        url = urljoin(HOME_URL, href) if href else ""
        # the homepage's per-row links carry a short-lived session token,
        # so they're useless as a stable identity -- key on the content
        key = f"{title}|{ref_no}|{closing_date}"
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "title": title,
                "url": url,
                "ref_no": ref_no,
                "closing_date": closing_date,
                "opening_date": opening_date,
                "key": key,
            }
        )
    return items


def is_construction_related(title: str) -> bool:
    haystack = title.lower()
    return any(kw in haystack for kw in CONSTRUCTION_KEYWORDS)


# ---------------------------------------------------------------------------
# Document fetching
# ---------------------------------------------------------------------------

DOC_LINK_HINTS = ["download", "attach", "notice", "document", ".pdf", "nit"]


def find_document_urls(session: requests.Session, detail_page_url: str) -> list[str]:
    """
    Best-effort: open the tender's detail page (using the SAME session
    that fetched the homepage, so any session cookie carries over) and
    look for links that plausibly point to the tender's document. The
    portal's exact markup wasn't verifiable ahead of time -- this is
    the piece most likely to need adjustment after seeing a real run's
    logs against the live site.
    """
    if not detail_page_url:
        return []
    try:
        resp = session.get(detail_page_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  could not open detail page: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(strip=True).lower()
        haystack = f"{href} {label}".lower()
        if any(hint in haystack for hint in DOC_LINK_HINTS):
            full = urljoin(detail_page_url, href)
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


def download_bytes(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


def fetch_pdf(session: requests.Session, detail_page_url: str):
    for url in find_document_urls(session, detail_page_url):
        try:
            content = download_bytes(session, url)
        except requests.RequestException as e:
            print(f"  failed to download {url}: {e}", file=sys.stderr)
            continue
        if content[:4] == b"%PDF":
            return content
    return None


# ---------------------------------------------------------------------------
# Text extraction (text layer first, OCR fallback for scans)
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_bytes: bytes) -> str:
    text = ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"  pypdf extraction failed: {e}", file=sys.stderr)

    if len(text.strip()) < 200:
        print("  little/no text layer -- falling back to OCR")
        text = ocr_pdf(pdf_bytes)
    return text


def ocr_pdf(pdf_bytes: bytes) -> str:
    try:
        # imported lazily -- these pull in poppler/tesseract bindings
        # that are only needed for the OCR fallback path
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as e:
        print(f"  OCR dependencies not available: {e}", file=sys.stderr)
        return ""
    try:
        images = convert_from_bytes(pdf_bytes, dpi=200)
    except Exception as e:
        print(f"  OCR page render failed: {e}", file=sys.stderr)
        return ""
    chunks = []
    for img in images[:MAX_OCR_PAGES]:
        try:
            chunks.append(pytesseract.image_to_string(img))
        except Exception as e:
            print(f"  OCR failed on a page: {e}", file=sys.stderr)
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize_with_claude(api_key: str, listing: dict, doc_text: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    doc_text = (doc_text or "").strip()[:MAX_DOC_CHARS]

    if doc_text:
        source_note = "Text extracted from the tender's attached document (may include OCR errors):"
        body = doc_text
    else:
        source_note = (
            'No document text could be retrieved -- base your answer only on '
            'the listing info above, and set confidence to "low".'
        )
        body = "(no document text available)"

    prompt = f"""You are helping a civil construction contractor in Odisha, India quickly decide whether a government tender notice is worth their attention.

Listing info already known:
- Title: {listing['title']}
- Reference No: {listing['ref_no']}
- Closing date: {listing['closing_date']}
- Bid opening date: {listing['opening_date']}

{source_note}
---
{body}
---

Respond with ONLY a JSON object (no markdown fences, no commentary) with exactly these keys:
{{
  "scope": "one sentence describing the work",
  "estimated_value": "tender/contract value if stated, else 'not stated'",
  "emd_amount": "earnest money deposit if stated, else 'not stated'",
  "eligibility": "one sentence on contractor class/experience required, else 'not stated'",
  "department_or_location": "issuing department and district/location if identifiable",
  "verdict": "one sentence: is this worth a civil construction contractor's attention, and why",
  "confidence": "high, medium, or low -- how complete/clear the source was"
}}"""

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("  Claude response wasn't valid JSON, falling back to raw text", file=sys.stderr)
        return {
            "scope": raw[:300],
            "estimated_value": "not available",
            "emd_amount": "not available",
            "eligibility": "not available",
            "department_or_location": "not available",
            "verdict": "Could not parse a structured summary -- check the PDF manually.",
            "confidence": "low",
        }


# ---------------------------------------------------------------------------
# Dashboard data (docs/data/index.json + docs/data/pdfs/*)
# ---------------------------------------------------------------------------

def load_index() -> list:
    if INDEX_FILE.exists():
        return json.loads(INDEX_FILE.read_text())
    return []


def save_index(records: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(records, indent=2, ensure_ascii=False))


def record_id_for(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def save_pdf(record_id: str, pdf_bytes: bytes) -> str:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    path = PDF_DIR / f"{record_id}.pdf"
    path.write_bytes(pdf_bytes)
    return f"data/pdfs/{record_id}.pdf"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()


def format_telegram_brief(record: dict) -> str:
    kind_label = "New Tender" if record["kind"] == "tender" else "New Corrigendum"
    lines = [
        f"\U0001F195 <b>{kind_label}</b> ({record.get('confidence', 'n/a')} confidence)",
        f"{record['title']}",
        f"Scope: {record.get('scope', 'not stated')}",
        f"Value: {record.get('estimated_value', 'not stated')} | EMD: {record.get('emd_amount', 'not stated')}",
        f"Eligibility: {record.get('eligibility', 'not stated')}",
        f"Closes: {record['closing_date']}",
        f"Verdict: {record.get('verdict', '')}",
    ]
    if record.get("pdf_path"):
        lines.append("(PDF saved to dashboard)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State (dedup memory across runs)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"tenders": [], "corrigendums": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Processing a single matched item end-to-end
# ---------------------------------------------------------------------------

def process_matched_item(session, api_key, item, kind) -> dict:
    print("  fetching document...")
    pdf_bytes = fetch_pdf(session, item["url"])

    if pdf_bytes:
        print(f"  extracting text ({len(pdf_bytes)} bytes)...")
        doc_text = extract_pdf_text(pdf_bytes)
    else:
        print("  no PDF found -- summarizing from listing info only")
        doc_text = ""

    print("  summarizing with Claude...")
    summary = summarize_with_claude(api_key, item, doc_text)

    rid = record_id_for(item["key"])
    pdf_path = save_pdf(rid, pdf_bytes) if pdf_bytes else None

    record = {
        "id": rid,
        "kind": kind,
        "title": item["title"],
        "ref_no": item["ref_no"],
        "closing_date": item["closing_date"],
        "opening_date": item["opening_date"],
        "portal_url": item["url"],
        "pdf_path": pdf_path,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **summary,
    }
    return record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    missing = [
        name
        for name, val in [
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
            ("ANTHROPIC_API_KEY", api_key),
        ]
        if not val
    ]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    session = requests.Session()
    html = fetch_html(session)
    soup = BeautifulSoup(html, "html.parser")

    tenders = extract_section(soup, "Tender Title")
    corrigendums = extract_section(soup, "Corrigendum Title")

    if not tenders and not corrigendums:
        print(
            "Warning: found 0 tenders and 0 corrigendums -- page structure may "
            "have changed, or the fetch was blocked. Not touching state.json.",
            file=sys.stderr,
        )
        return 1

    state = load_state()
    seen_tenders = set(state.get("tenders", []))
    seen_corrs = set(state.get("corrigendums", []))
    first_run = not seen_tenders and not seen_corrs

    new_tenders = [t for t in tenders if t["key"] not in seen_tenders]
    new_corrs = [c for c in corrigendums if c["key"] not in seen_corrs]

    index_records = load_index()
    processed, matched, alerted = 0, 0, 0

    for item, kind in [(t, "tender") for t in new_tenders] + [
        (c, "corrigendum") for c in new_corrs
    ]:
        processed += 1
        if not is_construction_related(item["title"]):
            continue
        matched += 1
        print(f"processing ({kind}): {item['title'][:70]}")
        try:
            record = process_matched_item(session, api_key, item, kind)
        except Exception as e:
            print(f"  failed to process this item, skipping: {e}", file=sys.stderr)
            continue

        index_records.insert(0, record)

        if not first_run:
            try:
                message = format_telegram_brief(record)
                for i in range(0, len(message), TELEGRAM_CHUNK):
                    send_telegram(token, chat_id, message[i : i + TELEGRAM_CHUNK])
                alerted += 1
            except requests.RequestException as e:
                print(f"  failed to send Telegram alert: {e}", file=sys.stderr)

    save_index(index_records[:MAX_INDEX_RECORDS])

    print(
        f"Run summary: {processed} new item(s) seen, {matched} construction-related, "
        f"{alerted} Telegram alert(s) sent."
        + (" (first run -- seeding only, no alerts)" if first_run else "")
    )

    all_tender_keys = list(seen_tenders | {t["key"] for t in tenders})[-MAX_KEYS_KEPT:]
    all_corr_keys = list(seen_corrs | {c["key"] for c in corrigendums})[-MAX_KEYS_KEPT:]
    state["tenders"] = all_tender_keys
    state["corrigendums"] = all_corr_keys
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
