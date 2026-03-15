#!/usr/bin/env python3
"""
Apartment Finder Agent
======================
Searches for new 2BR luxury apartments in Jersey City, NJ and emails a digest
to you and your husband. Runs 2x daily via GitHub Actions; tracks seen listings
so only truly new ones are ever emailed.
"""

import anthropic
import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel
from tavily import TavilyClient

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY")
GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAILS   = [e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()]

SEEN_LISTINGS_FILE = Path("seen_listings.json")

SEARCH_CRITERIA = """
APARTMENT SEARCH CRITERIA
- City: Jersey City, NJ
- Bedrooms: 2
- Max rent: $6,000/month (see budget nuance in ranking criteria)
- Move-in: June 2026 or early July 2026 (June 1 preferred)
- Must-haves: in-unit washer/dryer AND doorman/concierge
- Style: luxury buildings strongly preferred
- Location: must be within a 10-minute walk of one of these PATH stations:
    * Grove Street PATH
    * Exchange Place PATH
    * Newport PATH
- Preferred neighborhoods: Downtown JC, Grove Street, Newport, Paulus Hook, Exchange Place
"""

RANKING_CRITERIA = """
RANKING PREFERENCES (what makes a great pick):

BUDGET RULES — apply these carefully when scoring:
- Listings with unique/distinctive layouts AND modern kitchen+bathroom finishes:
  budget ceiling is $6,000/month — these are worth a premium
- Listings with standard/cookie-cutter layouts (typical open-plan rectangle,
  builder-grade finishes, nothing distinctive): budget ceiling is $5,500/month.
  Penalize anything over $5,500 with a standard layout by at least 2 score points.

UNIT QUALITY (most important factors):
1. Spacious feel — large square footage, open sightlines, generous room sizes
2. Natural sunlight — south/east/west facing, large windows, corner units especially prized
3. High floor — 7th floor or above strongly preferred; below 5th floor is a drawback
4. Unique or distinctive layout — split bedrooms, loft/duplex/multi-floor units,
   angled walls, terraces, extra-wide units, flex rooms, double-height ceilings,
   mezzanine levels; penalize generic single-floor rectangular layouts
5. Modern kitchen finishes — quartz/stone counters, high-end appliances (Sub-Zero,
   Wolf, Bosch), custom cabinetry, island or peninsula
6. Modern bathroom finishes — walk-in shower, soaking tub, double vanity,
   heated floors, designer tile

LOCATION & BUILDING:
7. Proximity to PATH station — under 5 min walk is ideal
8. Doorman / concierge building (required)
9. In-unit washer/dryer (required)
10. Building amenities: rooftop deck, gym, package handling, bike storage
11. NYC skyline or waterfront views (significant bonus, especially from high floors)
12. Parking included (nice to have)
13. Pet-friendly (nice to have)
14. Availability: June 1 ideal, early July acceptable
"""

# Searches to run every cycle. Kept to ~10 to stay well within Tavily's free tier
# (1,000 credits/month; basic search = 1 credit; 10 searches × 60 runs = 600 credits).
SEARCH_QUERIES = [
    # General rental-site searches
    "2 bedroom luxury apartment Jersey City NJ doorman in-unit laundry June 2026",
    "2 bedroom apartment Jersey City NJ Grove Street Exchange Place PATH rent 2026",
    "Newport Paulus Hook Jersey City 2BR luxury apartment available 2026",
    "Downtown Jersey City luxury high-rise 2 bedroom rental 2026 streeteasy zillow",
    "Jersey City NJ 2 bedroom apartment renthop hotpads trulia 2026 doorman laundry",
    # Target-building searches
    "The Lively Jersey City 2 bedroom apartment available rent 2026",
    "Haus25 Jersey City 2 bedroom rental available 2026",
    "VYV BLVD 401 425 475 Quinn Lenox Jersey City 2BR apartment",
    "90 Columbus 351 Marin The Hendrix Jersey City rental 2026",
    "151 Bay Street Jersey City luxury 2 bedroom available 2026",
]


# ---------------------------------------------------------------------------
# Pydantic models for structured extraction
# ---------------------------------------------------------------------------
class ApartmentListing(BaseModel):
    listing_id: str              # URL or unique string — used for de-duplication
    url: str
    address: str
    building_name: Optional[str] = None
    price: int                   # monthly rent in USD
    bedrooms: int
    bathrooms: Optional[float] = None
    sqft: Optional[int] = None
    floor: Optional[str] = None          # e.g. "12th floor", "high floor", "floors 8-15"
    layout_type: Optional[str] = None    # e.g. "split 2BR", "duplex/loft", "corner unit", "standard open plan"
    sunlight: Optional[str] = None       # e.g. "south-facing", "corner with floor-to-ceiling windows"
    finishes: Optional[str] = None       # e.g. "quartz counters, Sub-Zero fridge, spa bath"
    amenities: list[str]
    available_date: Optional[str] = None
    walk_to_path: Optional[str] = None   # e.g. "4-min walk to Grove St PATH"
    source: str                  # apartments.com, zillow, streeteasy, etc.
    notes: Optional[str] = None
    score: int                   # 1–10 fit score
    score_reason: str            # one-sentence explanation


class SearchResults(BaseModel):
    listings: list[ApartmentListing]
    search_summary: str          # 2–3 sentence overview of what was found
    top_pick_ids: list[str]      # listing_ids of the top 1–3 picks


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def load_seen_listings() -> dict:
    if SEEN_LISTINGS_FILE.exists():
        return json.loads(SEEN_LISTINGS_FILE.read_text())
    return {}


def save_seen_listings(seen: dict) -> None:
    SEEN_LISTINGS_FILE.write_text(json.dumps(seen, indent=2))


# ---------------------------------------------------------------------------
# Stage 1 — Tavily web search
# ---------------------------------------------------------------------------
def search_for_apartments(seen_ids: list[str]) -> str:
    """
    Run targeted Tavily searches across rental sites and specific buildings.
    Returns a compiled plain-text report for Stage 2 (Claude extraction).
    """
    tavily = TavilyClient(api_key=TAVILY_API_KEY)

    report_parts = [
        "RAW APARTMENT SEARCH RESULTS",
        f"Search run: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "SEARCH CRITERIA:",
        SEARCH_CRITERIA.strip(),
        "",
        f"Already-seen listing IDs to SKIP:\n{json.dumps(seen_ids[:150])}",
        "",
    ]

    for query in SEARCH_QUERIES:
        report_parts.append(f"\n{'='*60}")
        report_parts.append(f"QUERY: {query}")
        report_parts.append("="*60)

        try:
            resp = tavily.search(
                query=query,
                search_depth="basic",
                max_results=6,
                include_raw_content=True,
            )
            results = resp.get("results", [])
            if not results:
                report_parts.append("No results returned.")
                continue

            for r in results:
                report_parts.append("\n--- Result ---")
                report_parts.append(f"Title:   {r.get('title', 'N/A')}")
                report_parts.append(f"URL:     {r.get('url', 'N/A')}")
                report_parts.append(f"Snippet: {r.get('content', '')}")
                raw = r.get("raw_content") or ""
                if raw:
                    # Cap per-page content to keep the total report manageable
                    report_parts.append(f"Page content:\n{raw[:2500]}")

        except Exception as e:
            report_parts.append(f"Search failed: {e}")

    return "\n".join(report_parts)


# ---------------------------------------------------------------------------
# Stage 2 — Structured extraction + ranking (Claude)
# ---------------------------------------------------------------------------
def extract_and_rank_listings(
    client: anthropic.Anthropic,
    search_text: str,
    seen_ids: list[str],
) -> Optional[SearchResults]:
    """
    Parse the raw search report into structured ApartmentListing objects,
    score each one, and identify the top picks.
    """
    system = f"""You are extracting and ranking apartment listings from a web search report.

{SEARCH_CRITERIA}

{RANKING_CRITERIA}

Extract each distinct listing into the structured format. Exclude any listing whose
URL appears in the already-seen list. Only include listings where an actual price is
shown — skip any that say "inquire", "call for pricing", or have no rent listed.

Scoring guide (1–10):
 9–10 — Perfect: meets ALL criteria, great PATH proximity, luxury building
 7–8  — Great: meets requirements, solid location
 5–6  — Good: meets most requirements, acceptable location
 3–4  — Marginal: missing a requirement but worth knowing about
 1–2  — Poor: missing key requirements

After scoring, choose the top 1–3 listings and add their listing_ids to top_pick_ids.
"""

    response = client.messages.parse(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=system,
        messages=[{
            "role": "user",
            "content": (
                "Extract all apartment listings from the search report below into "
                "structured format. Score each 1–10 and identify the top picks.\n\n"
                f"Already-seen listing IDs to EXCLUDE:\n{json.dumps(seen_ids[:150])}\n\n"
                f"Search report:\n\n{search_text}"
            ),
        }],
        output_format=SearchResults,
    )

    return response.parsed_output


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------
def _listing_html(listing: ApartmentListing, is_top_pick: bool) -> str:
    badge  = '<span class="badge">⭐ TOP PICK</span>' if is_top_pick else ""
    css    = "listing top-pick" if is_top_pick else "listing"

    amenity_tags = "".join(
        f'<span class="tag">{a}</span>' for a in listing.amenities[:10]
    )
    amenities_html = f'<div class="amenities">{amenity_tags}</div>' if amenity_tags else ""

    sqft_str    = f" &bull; {listing.sqft:,} sqft" if listing.sqft else ""
    bath_str    = f" &bull; {listing.bathrooms} ba" if listing.bathrooms else ""
    floor_str   = f"<div class='detail'>🏢 {listing.floor}</div>" if listing.floor else ""
    layout_str  = f"<div class='detail'>📐 {listing.layout_type}</div>" if listing.layout_type else ""
    sun_str     = f"<div class='detail'>☀️ {listing.sunlight}</div>" if listing.sunlight else ""
    finish_str  = f"<div class='detail'>✨ {listing.finishes}</div>" if listing.finishes else ""
    avail_str   = f"<div class='detail'>📅 Available: {listing.available_date}</div>" if listing.available_date else ""
    path_str    = f"<div class='detail path'>🚇 {listing.walk_to_path}</div>" if listing.walk_to_path else ""
    notes_str   = f"<div class='detail muted'>📝 {listing.notes}</div>" if listing.notes else ""
    name        = listing.building_name or listing.address

    return f"""
<div class="{css}">
  <div class="listing-header">
    <h3><a href="{listing.url}">{name}</a> {badge}</h3>
    <span class="score">{listing.score}/10</span>
  </div>
  <div class="detail muted">{listing.address} &bull; via {listing.source}</div>
  <div class="price">${listing.price:,}/mo</div>
  <div class="detail">{listing.bedrooms} bed{bath_str}{sqft_str}</div>
  {floor_str}
  {layout_str}
  {sun_str}
  {finish_str}
  {amenities_html}
  {path_str}
  {avail_str}
  {notes_str}
  <div class="score-reason">💬 {listing.score_reason}</div>
</div>
"""


def format_email_html(results: SearchResults) -> str:
    top_ids    = set(results.top_pick_ids)
    top_picks  = sorted([l for l in results.listings if l.listing_id in top_ids],    key=lambda x: x.score, reverse=True)
    others     = sorted([l for l in results.listings if l.listing_id not in top_ids], key=lambda x: x.score, reverse=True)

    now = datetime.now().strftime("%A, %B %d at %-I:%M %p")
    n   = len(results.listings)

    top_section = ""
    if top_picks:
        cards = "\n".join(_listing_html(l, True) for l in top_picks)
        top_section = f"<h2>⭐ Top Picks ({len(top_picks)})</h2>\n{cards}"

    other_section = ""
    if others:
        cards = "\n".join(_listing_html(l, False) for l in others)
        other_section = f"<h2>All New Listings ({len(others)})</h2>\n{cards}"

    no_listings = '<p class="muted">No new listings this run — will check again soon!</p>' if not results.listings else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body        {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                 max-width: 680px; margin: 0 auto; padding: 24px; color: #1a1a2e; }}
  h1          {{ color: #16213e; border-bottom: 3px solid #0f3460; padding-bottom: 10px; }}
  h2          {{ color: #0f3460; margin-top: 32px; }}
  .summary    {{ background: #e8f4f8; padding: 14px 18px; border-radius: 8px;
                 margin-bottom: 24px; color: #16213e; line-height: 1.5; }}
  .listing    {{ background: #f7f9fc; border-left: 4px solid #0f3460;
                 padding: 16px 18px; margin: 14px 0; border-radius: 0 10px 10px 0; }}
  .top-pick   {{ background: #fffbeb; border-left: 4px solid #e9a825; }}
  .listing-header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }}
  .listing h3 {{ margin: 0; font-size: 1.05em; }}
  .listing a  {{ color: #0f3460; text-decoration: none; font-weight: 700; }}
  .listing a:hover {{ text-decoration: underline; }}
  .price      {{ font-size: 1.3em; color: #1a7a4a; font-weight: 700; margin: 6px 0; }}
  .score      {{ background: #1a7a4a; color: #fff; padding: 4px 10px;
                 border-radius: 20px; font-weight: 700; font-size: 0.9em;
                 white-space: nowrap; flex-shrink: 0; }}
  .badge      {{ background: #e9a825; color: #fff; padding: 2px 8px;
                 border-radius: 10px; font-size: 0.8em; margin-left: 6px; }}
  .amenities  {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0; }}
  .tag        {{ background: #dbeafe; color: #1d4ed8; padding: 3px 8px;
                 border-radius: 10px; font-size: 0.82em; }}
  .detail     {{ margin: 4px 0; font-size: 0.92em; }}
  .path       {{ color: #7c3aed; font-weight: 500; }}
  .muted      {{ color: #6b7280; }}
  .score-reason {{ font-style: italic; color: #555; font-size: 0.88em; margin-top: 8px; }}
  hr          {{ border: none; border-top: 1px solid #e5e7eb; margin: 32px 0; }}
  footer      {{ color: #9ca3af; font-size: 0.82em; text-align: center; }}
</style>
</head>
<body>
  <h1>🏠 Jersey City Apartment Alert</h1>
  <p class="muted">{now} &bull; {n} new listing{"s" if n != 1 else ""} found</p>
  <div class="summary"><strong>Summary:</strong> {results.search_summary}</div>
  {top_section}
  {other_section}
  {no_listings}
  <hr>
  <footer>Sent by your Apartment Finder Agent &bull; Runs twice daily</footer>
</body>
</html>
"""


def format_email_plain(results: SearchResults) -> str:
    lines = [
        f"Jersey City Apartment Alert — {datetime.now().strftime('%b %d, %Y')}",
        f"{len(results.listings)} new listing(s) found\n",
        results.search_summary,
        "",
    ]
    top_ids = set(results.top_pick_ids)
    for l in sorted(results.listings, key=lambda x: x.score, reverse=True):
        star   = "⭐ TOP PICK — " if l.listing_id in top_ids else ""
        extras = " | ".join(filter(None, [l.floor, l.layout_type, l.sunlight]))
        lines += [
            f"{star}{l.address}",
            f"  ${l.price:,}/mo | Score: {l.score}/10" + (f" | {extras}" if extras else ""),
            f"  {l.url}",
            f"  {l.score_reason}",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------
def send_email(results: SearchResults) -> None:
    n = len(results.listings)
    subject = f"🏠 {n} New Jersey City Apartment{'s' if n != 1 else ''} Found!"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ", ".join(RECIPIENT_EMAILS)

    msg.attach(MIMEText(format_email_plain(results), "plain"))
    msg.attach(MIMEText(format_email_html(results),  "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAILS, msg.as_string())

    print(f"  ✓ Email sent to: {', '.join(RECIPIENT_EMAILS)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n[{ts}] Apartment Finder starting...")

    # Validate config
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY":  ANTHROPIC_API_KEY,
        "TAVILY_API_KEY":     TAVILY_API_KEY,
        "GMAIL_ADDRESS":      GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
        "RECIPIENT_EMAILS":   RECIPIENT_EMAILS,
    }.items() if not v]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load previously seen listings (keyed by listing_id / URL)
    seen = load_seen_listings()
    seen_ids = list(seen.keys())
    print(f"  Already tracked: {len(seen_ids)} listing(s)")

    # Stage 1 — Tavily search
    print("  Searching the web for new listings via Tavily...")
    search_text = search_for_apartments(seen_ids)

    # Stage 2 — Claude: extract + rank
    print("  Extracting and ranking listings...")
    results = extract_and_rank_listings(client, search_text, seen_ids)

    if results is None:
        print("  ⚠ Could not extract structured listings. Check the logs.")
        return

    n_new = len(results.listings)
    print(f"  Found {n_new} new listing(s).")

    if n_new == 0:
        print("  No new listings — skipping email.")
        return

    # Update seen listings
    for listing in results.listings:
        seen[listing.listing_id] = {
            "address":    listing.address,
            "price":      listing.price,
            "url":        listing.url,
            "first_seen": datetime.now().isoformat(),
        }
    save_seen_listings(seen)

    # Send email
    print("  Sending email digest...")
    send_email(results)

    print(f"[{datetime.now().strftime('%H:%M')}] Done.\n")


if __name__ == "__main__":
    main()
