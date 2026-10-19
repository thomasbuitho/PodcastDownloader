#!/usr/bin/env python3
"""
Podcast Downloader
Downloads episodes from an RSS feed, organized by season.

Usage:
    python3 podcast_downloader.py
    python3 podcast_downloader.py --dry-run         # Preview without downloading
    python3 podcast_downloader.py --season 2        # Only download season 2
    python3 podcast_downloader.py --limit 5         # Download only the 5 most recent episodes
    python3 podcast_downloader.py --output ~/Podcasts/Floodcast
"""

import argparse
import html
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path


# ── Configuration ────────────────────────────────────────────────────────────

FEED_URL   = "https://feeds.acast.com/public/shows/floodcast"
OUTPUT_DIR = Path("./Podcasts/Floodcast").expanduser()

# Namespaces used in iTunes/podcast RSS feeds
NS = {
    "itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "http://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    """Remove or replace characters that are invalid in file/folder names."""
    text = text.strip()
    # Replace path separators and other problematic chars
    text = re.sub(r'[/\\:*?"<>|]', "-", text)
    # Collapse multiple spaces/dashes
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-{2,}", "-", text)
    return text[:120]  # cap length


def first_sentence(raw: str) -> str:
    """
    Extract the first meaningful sentence from a (possibly HTML) description.
    Returns an empty string if nothing useful is found.
    """
    if not raw:
        return ""

    # Decode HTML entities (&amp; &lt; etc.)
    text = html.unescape(raw)

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Collapse whitespace / newlines
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text).strip()

    # Episodes always start with "Avec … ." (period with no space after)
    # Capture everything from "Avec" up to and including the first period.
    match = re.match(r"^Avec (.+?\.)", text)
    if match:
        sentence = match.group(1).strip()
        # Drop the trailing period — cleaner in a filename
        sentence = sentence.rstrip(".")
    else:
        # Fallback: take up to the first period or 120 chars
        match = re.match(r"^(.+?[.!?])(?:\s|$)", text)
        sentence = match.group(1).rstrip(".!?") if match else text[:120]

    return sanitize(sentence)


def fetch_feed(url: str) -> bytes:
    """Download the RSS feed with a browser-like User-Agent."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_int(text: str, default: int = 0) -> int:
    try:
        return int(text) if text else default
    except ValueError:
        return default


def format_date(pub_date_str: str) -> str:
    """Parse an RFC 2822 date string and return YYYY-MM-DD."""
    if not pub_date_str:
        return "0000-00-00"
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def get_text(element, tag: str, ns_key: str = None) -> str:
    """Safely extract text from a child element (with optional namespace)."""
    if ns_key:
        child = element.find(f"{ns_key}:{tag}", NS)
    else:
        child = element.find(tag)
    return child.text if child is not None else None


def progress_bar(downloaded: int, total: int, bar_width: int = 40) -> str:
    if total <= 0:
        return f"{downloaded} bytes"
    pct = downloaded / total
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)
    mb_done  = downloaded / 1_048_576
    mb_total = total      / 1_048_576
    return f"[{bar}] {pct:5.1%}  {mb_done:.1f}/{mb_total:.1f} MB"


def download_file(url: str, dest: Path, dry_run: bool = False) -> bool:
    """
    Download *url* to *dest*.  Returns True on success.
    Skips silently if the file already exists.
    """
    if dest.exists():
        print(f"  ⏭  Already exists, skipping.")
        return True

    if dry_run:
        print(f"  📋  [dry-run] Would download → {dest}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 1024 * 256  # 256 KB

            with open(tmp, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    print(f"\r  {progress_bar(downloaded, total)}", end="", flush=True)

        print()  # newline after progress bar
        tmp.rename(dest)
        return True

    except Exception as exc:
        print(f"\n  ❌  Download failed: {exc}")
        if tmp.exists():
            tmp.unlink()
        return False


# ── Core logic ───────────────────────────────────────────────────────────────

def parse_episodes(xml_bytes: bytes) -> list[dict]:
    """Parse the RSS XML and return a list of episode dicts, sorted by season then episode."""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("No <channel> element found in feed.")

    episodes = []

    for item in channel.findall("item"):
        title     = get_text(item, "title") or "Untitled"
        pub_date  = get_text(item, "pubDate")
        season    = parse_int(get_text(item, "season",  "itunes"), default=0)
        ep_num    = parse_int(get_text(item, "episode", "itunes"), default=0)
        ep_type   = get_text(item, "episodeType", "itunes") or "full"

        # Description: prefer <content:encoded> (richest), then <itunes:summary>, then <description>
        raw_desc = (
            get_text(item, "encoded", "content")
            or get_text(item, "summary", "itunes")
            or get_text(item, "description")
        )
        guest_hint = first_sentence(raw_desc)

        # Audio URL is in <enclosure url="..." />
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None

        if not audio_url:
            continue  # skip items with no audio

        # Strip query-string tracking params that acast appends
        audio_url_clean = audio_url.split("?")[0]
        # Determine extension
        ext = Path(audio_url_clean).suffix or ".mp3"
        if ext not in {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wav"}:
            ext = ".mp3"

        episodes.append({
            "title":     title,
            "pub_date":  pub_date,
            "date_str":  format_date(pub_date),
            "season":    season,
            "ep_num":    ep_num,
            "ep_type":   ep_type,
            "guest_hint": guest_hint,
            "audio_url": audio_url,       # original (may have tracking)
            "ext":       ext,
        })

    # Sort: season asc, then episode asc (0 = unknown, goes last)
    episodes.sort(key=lambda e: (
        e["season"] if e["season"] > 0 else 9999,
        e["ep_num"]  if e["ep_num"]  > 0 else 9999,
        e["date_str"],
    ))

    return episodes


def build_filename(ep: dict) -> str:
    """
    Build a filename like:
        S02E05 - 2024-03-15 - Episode Title - First sentence of description.mp3
    Episodes with season=0 or ep_num=0 use a simpler fallback.
    """
    title = sanitize(ep["title"])
    date  = ep["date_str"]
    guest = ep["guest_hint"]  # first sentence of description, already sanitized

    # if ep["season"] > 0 and ep["ep_num"] > 0:
    #     prefix = f"S{ep['season']:02d}E{ep['ep_num']:02d} - {date} - "
    # elif ep["ep_num"] > 0:
    #     prefix = f"E{ep['ep_num']:03d} - {date} - "
    # else:
    prefix = f"{date} - "

    name = prefix + title
    if guest and guest.lower() != title.lower():
        name += f" - {guest}"
    name += ep["ext"]

    return name


def season_folder(ep: dict, base: Path) -> Path:
    if ep["season"] > 0:
        return base / f"Season {ep['season']:02d}"
    return base / "Unsorted"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download Floodcast podcast episodes organised by season."
    )
    parser.add_argument("--output",   default=str(OUTPUT_DIR), help="Root download folder")
    parser.add_argument("--season",   type=int, default=None,  help="Only download this season number")
    parser.add_argument("--limit",    type=int, default=None,  help="Max number of episodes to download")
    parser.add_argument("--dry-run",  action="store_true",     help="Show what would be downloaded without doing it")
    parser.add_argument("--url",      default=FEED_URL,        help="RSS feed URL")
    args = parser.parse_args()

    output_root = Path(args.output).expanduser()

    print(f"📡  Fetching RSS feed from {args.url} …")
    try:
        xml_bytes = fetch_feed(args.url)
    except Exception as exc:
        print(f"❌  Could not fetch feed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("🔍  Parsing episodes …")
    try:
        episodes = parse_episodes(xml_bytes)
    except Exception as exc:
        print(f"❌  Could not parse feed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Apply filters
    if args.season is not None:
        episodes = [e for e in episodes if e["season"] == args.season]

    if args.limit is not None:
        episodes = episodes[:args.limit]

    if not episodes:
        print("⚠️  No episodes matched your filters.")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"  Found {len(episodes)} episode(s) to process")
    print(f"  Output → {output_root}")
    if args.dry_run:
        print("  Mode   → DRY RUN (nothing will be written)")
    print(f"{'='*60}\n")

    ok = fail = skip = 0

    for i, ep in enumerate(episodes, 1):
        folder   = season_folder(ep, output_root)
        filename = build_filename(ep)
        dest     = folder / filename

        print(f"[{i}/{len(episodes)}] S{ep['season']:02d}E{ep['ep_num']:02d}  {ep['title']}")
        print(f"  📁  {dest.relative_to(output_root.parent) if output_root.parent != output_root else dest}")

        if dest.exists():
            print("  ⏭  Already downloaded, skipping.")
            skip += 1
            continue

        success = download_file(ep["audio_url"], dest, dry_run=args.dry_run)
        if success:
            ok += 1
        else:
            fail += 1

        # Small polite pause between requests
        if i < len(episodes) and not args.dry_run and not dest.exists():
            time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  ✅  Downloaded : {ok}")
    print(f"  ⏭  Skipped    : {skip}")
    print(f"  ❌  Failed     : {fail}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()