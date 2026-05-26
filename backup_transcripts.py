#!/usr/bin/env python3
"""
Daily backup script for Granola meeting transcripts.

Pulls from Granola's public REST API (https://public-api.granola.ai/v1)
using a static bearer token. Replaced the cache-reading version on
2026-05-01 — Granola stopped storing transcripts in the local cache.

Auth: GRANOLA_API_KEY env var (sourced from ~/.env) — `grn_*` token.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================

VAULT_ROOT = os.path.expanduser("~/Documents/Manual Library/grey-rose")
WORK_OUTPUT_DIR = os.path.join(VAULT_ROOT, "Projects/MaidCentral/Meetings")
PERSONAL_OUTPUT_DIR = os.path.join(VAULT_ROOT, "Projects/Personal/Meetings")

API_BASE = "https://public-api.granola.ai/v1"
STATUS_FILE = os.path.expanduser("~/.granola-backup-status.json")
TIMEZONE = os.getenv("GRANOLA_TIMEZONE", None)
DRY_RUN = "--dry-run" in sys.argv
BACKFILL = "--backfill" in sys.argv  # ignore last_synced_at, pull everything

PAGE_SIZE = 30  # API max
SUSTAINED_RPS = 5  # Granola: 5 req/s sustained — we throttle conservatively
REQUEST_DELAY = 1.0 / SUSTAINED_RPS


def _load_env_token():
    """Read GRANOLA_API_KEY from environment or ~/.env."""
    token = os.getenv("GRANOLA_API_KEY")
    if token:
        return token
    env_path = os.path.expanduser("~/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GRANOLA_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


GRANOLA_API_KEY = _load_env_token()


# ============================================================================
# Status tracking
# ============================================================================

def write_status(success, saved_count=0, error_msg=None, last_updated_at=None):
    now = datetime.now(timezone.utc).isoformat()
    status = {"last_run": now, "success": success, "saved_count": saved_count}
    prev = _read_status() or {}
    if saved_count > 0:
        status["last_saved"] = now
    elif prev.get("last_saved"):
        status["last_saved"] = prev["last_saved"]
    if last_updated_at:
        status["last_updated_at"] = last_updated_at
    elif prev.get("last_updated_at"):
        status["last_updated_at"] = prev["last_updated_at"]
    if error_msg:
        status["error"] = error_msg
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not write status file: {e}")


def _read_status():
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return None


# ============================================================================
# Meeting classification
# ============================================================================

PERSONAL_TITLE_PATTERNS = [
    r"therapy", r"martin", r"dr[\.\s]?hess", r"shell[\s._-]?game",
    r"mini[\s._-]?convo", r"project[\s._-]?martina",
    r"off[\s._-]?by[\s._-]?5", r"family[\s._-]?meeting", r"christina",
    r"dentist", r"vet\b", r"doctor", r"personal",
    r"leon.*ppo", r"lenny.*mercer", r"mentorship.*slot",
    # Tulip / pet medical (vet appointments often live on the MC calendar)
    r"\btulip\b", r"\bbutters\b", r"\blennon\b",
    r"\bimha\b", r"blue[\s._-]?pearl", r"dr[\W_]*davis",
    r"iv[\W_]*steroid", r"\bmri\b", r"blood[\W_]*issue",
    r"hospital[\W_]*stay", r"heaven",
    # Personal-life contacts (added 2026-05-26 after Cheryl + Tony misclassification)
    r"\bcheryl\b", r"\btony\b", r"\bquacy\b",
    r"harmony[\s._-]?house", r"\banya\b", r"\bjoliz\b",
]
WORK_PARTICIPANTS = [
    "tom", "amanda", "amandah", "austin", "alvin", "troy", "mark",
    "randall", "greg", "jon", "dena", "cece", "ajia", "victoria",
    "blake", "maks", "suhani", "yodit", "ryan", "arthur", "nathan", "sara",
]


def classify_meeting(title, participants):
    title_lower = (title or "Untitled Meeting").lower()
    for pattern in PERSONAL_TITLE_PATTERNS:
        if re.search(pattern, title_lower):
            return "personal"
    participant_names = [p.lower() for p in participants]
    for work_person in WORK_PARTICIPANTS:
        if any(work_person in name for name in participant_names):
            return "work"
    return "work"


def generate_tags(meeting_title, classification):
    tags = ["meeting", "personal" if classification == "personal" else "maidcentral"]
    title_lower = meeting_title.lower()
    keyword_tags = [
        (["revenue", "sales", "pipeline"], "revenue"),
        (["marketing", "content", "blog"], "marketing"),
        (["demo", "presentation"], "demo"),
        (["strategy", "planning"], "strategy"),
        (["weekly", "sync", "standup"], "recurring"),
        (["ai", "automation"], "ai"),
        (["1:1", "1-1", "<>"], "one-on-one"),
        (["therapy"], "therapy"),
        (["onboarding", "graduation"], "onboarding"),
        (["implementation"], "implementation"),
    ]
    for keywords, tag in keyword_tags:
        if any(w in title_lower for w in keywords):
            tags.append(tag)
    return tags


# ============================================================================
# Timezone detection
# ============================================================================

def detect_timezone():
    if TIMEZONE:
        try:
            return zoneinfo.ZoneInfo(TIMEZONE)
        except Exception:
            print(f"Warning: Invalid timezone '{TIMEZONE}', auto-detecting")
    try:
        if hasattr(time, "tzname") and time.tzname:
            tz_mapping = {
                "EST": "America/New_York", "EDT": "America/New_York",
                "CST": "America/Chicago", "CDT": "America/Chicago",
                "MST": "America/Denver", "MDT": "America/Denver",
                "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
            }
            current_tz = time.tzname[time.daylight]
            if current_tz in tz_mapping:
                return zoneinfo.ZoneInfo(tz_mapping[current_tz])
        local_offset = time.timezone if not time.daylight else time.altzone
        hours_offset = -local_offset // 3600
        offset_mapping = {
            -8: "America/Los_Angeles", -7: "America/Denver",
            -6: "America/Chicago", -5: "America/New_York", -4: "America/New_York",
        }
        if hours_offset in offset_mapping:
            return zoneinfo.ZoneInfo(offset_mapping[hours_offset])
    except Exception as e:
        print(f"Warning: timezone detection: {e}")
    return zoneinfo.ZoneInfo("UTC")


# ============================================================================
# REST API client
# ============================================================================

class GranolaAPIError(Exception):
    pass


def _api_get(path, params=None, retries=3):
    if not GRANOLA_API_KEY:
        raise GranolaAPIError("GRANOLA_API_KEY not set in env or ~/.env")
    url = f"{API_BASE}{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GRANOLA_API_KEY}",
        "Accept": "application/json",
    })
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  Rate limited, sleeping {wait}s...")
                time.sleep(wait)
                last_err = e
                continue
            if e.code in (502, 503, 504):
                wait = 2 ** attempt
                time.sleep(wait)
                last_err = e
                continue
            body = e.read().decode("utf-8", errors="replace")[:200]
            raise GranolaAPIError(f"HTTP {e.code} on {path}: {body}")
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise GranolaAPIError(f"Failed after {retries} retries: {last_err}")


def list_notes(updated_after=None):
    """Yield every note matching filter, paginating through cursors."""
    cursor = None
    while True:
        params = {"page_size": PAGE_SIZE}
        if updated_after:
            params["updated_after"] = updated_after
        if cursor:
            params["cursor"] = cursor
        data = _api_get("/notes", params)
        for note in data.get("notes", []):
            yield note
        if not data.get("hasMore"):
            return
        cursor = data.get("cursor")
        if not cursor:
            return
        time.sleep(REQUEST_DELAY)


def get_note(note_id, include_transcript=True):
    params = {}
    if include_transcript:
        params["include"] = "transcript"
    return _api_get(f"/notes/{note_id}", params)


# ============================================================================
# REST → internal dict mapping
# ============================================================================

def note_to_meeting(note):
    """Map a Note response into the meeting dict the save functions expect."""
    cal = note.get("calendar_event") or {}
    start_str = cal.get("scheduled_start_time") or note.get("created_at")
    if start_str:
        if start_str.endswith("Z"):
            start_str = start_str[:-1] + "+00:00"
        meeting_date = datetime.fromisoformat(start_str)
        if meeting_date.tzinfo is None:
            meeting_date = meeting_date.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
    else:
        meeting_date = datetime.now(zoneinfo.ZoneInfo("UTC"))

    participants = []
    for a in note.get("attendees") or []:
        name = (a.get("name") or "").strip()
        if name:
            participants.append(name)
    if not participants:
        for inv in cal.get("invitees") or []:
            email = inv.get("email", "")
            if email and not inv.get("self"):
                participants.append(email.split("@")[0].replace(".", " ").title())

    return {
        "id": note["id"],
        "title": note.get("title") or "Untitled Meeting",
        "date": meeting_date,
        "participants": participants,
        "type": "meeting",
        "web_url": note.get("web_url"),
    }


def note_to_transcript(note):
    """Build the transcript dict from a Note's transcript array."""
    segments = note.get("transcript") or []
    if not segments:
        return None
    parts = []
    speakers = set()
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
        spkr = seg.get("speaker")
        if isinstance(spkr, dict):
            spkr = spkr.get("source") or spkr.get("name")
        if spkr:
            speakers.add(str(spkr))
    if not parts:
        return None
    return {"content": " ".join(parts), "speakers": list(speakers)}


def note_to_summary_sections(note):
    """Map summary_markdown into the sections shape save_notes_to_file expects."""
    md = (note.get("summary_markdown") or "").strip()
    if not md:
        md = (note.get("summary_text") or "").strip()
    if len(md) < 50:
        return None
    return [{"title": "Summary", "content": md}]


# ============================================================================
# Filename / output helpers
# ============================================================================

def create_safe_title(meeting_title):
    safe = "".join(c for c in meeting_title if c.isalnum() or c in (" ", "-", "_")).strip()
    return safe.replace(" ", "_")[:100]


def create_safe_title_kebab(meeting_title):
    safe = "".join(c for c in meeting_title if c.isalnum() or c in (" ", "-", "_")).strip()
    return safe.replace(" ", "-").lower()[:50]


def get_output_dir(base_dir, meeting_date, local_tz):
    local_date = meeting_date.astimezone(local_tz)
    return Path(base_dir) / local_date.strftime("%Y") / local_date.strftime("%m")


_TRANSCRIPT_FNAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+?)\.md$")
_NOTES_FNAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)-notes\.md$")


def _normalize_title(title):
    """Lowercase, collapse non-alphanum runs to single space."""
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return re.sub(r"\s+", " ", t)


def _hhmm_to_minutes(s):
    return int(s[:2]) * 60 + int(s[2:4])


def file_exists_anywhere(base_dir, filename, time_window_min=10):
    """Match the exact filename OR a fuzzy twin in the same base_dir tree.

    Fuzzy match rules:
    - Same date AND normalized title → twin (any time)
    - Same date AND time within ±10 min → twin (covers title drift)
    - Same date AND normalized notes title → twin (for *-notes.md files)
    """
    base_path = Path(base_dir)
    target_t = _TRANSCRIPT_FNAME.match(filename)
    target_n = _NOTES_FNAME.match(filename)
    target_norm = None
    target_date = None
    target_time = None
    if target_t:
        target_date = target_t.group(1)
        target_time = target_t.group(2)
        target_norm = _normalize_title(target_t.group(3))
    elif target_n:
        target_date = target_n.group(1)
        target_norm = _normalize_title(target_n.group(2))

    if not base_path.exists():
        return False

    for path in base_path.rglob("*.md"):
        base = path.name
        if base == filename:
            return True
        if not target_date:
            continue
        m_t = _TRANSCRIPT_FNAME.match(base)
        m_n = _NOTES_FNAME.match(base)
        if target_t and m_t:
            cand_date, cand_time, cand_title = m_t.group(1), m_t.group(2), m_t.group(3)
            if cand_date != target_date:
                continue
            if _normalize_title(cand_title) == target_norm:
                return True
            if abs(_hhmm_to_minutes(cand_time) - _hhmm_to_minutes(target_time)) <= time_window_min:
                return True
        elif target_n and m_n:
            cand_date, cand_title = m_n.group(1), m_n.group(2)
            if cand_date != target_date:
                continue
            if _normalize_title(cand_title) == target_norm:
                return True
    return False


def build_related_links(meeting, local_tz, file_type):
    local_date = meeting["date"].astimezone(local_tz)
    date_str = local_date.strftime("%Y-%m-%d")
    time_str = local_date.strftime("%H%M")
    safe_underscore = create_safe_title(meeting["title"])
    safe_kebab = create_safe_title_kebab(meeting["title"])
    transcript_name = f"{date_str}_{time_str}_{safe_underscore}"
    notes_name = f"{date_str}-{safe_kebab}-notes"
    links = []
    if file_type != "transcript":
        links.append(f"- [[{transcript_name}|Transcript]]")
    if file_type != "notes":
        links.append(f"- [[{notes_name}|Notes]]")
    if not links:
        return ""
    return "\n---\n\n## Related\n\n" + "\n".join(links) + "\n"


def format_tags_inline(tags):
    return "tags: [" + ", ".join(tags) + "]"


def format_participants_frontmatter(participants):
    if not participants:
        return ""
    lines = ["participants:"]
    for p in participants:
        lines.append(f'  - "[[{p}]]"')
    return "\n".join(lines)


# ============================================================================
# Save functions
# ============================================================================

def save_transcript_to_file(meeting, transcript, base_dir, local_tz, classification):
    try:
        output_path = get_output_dir(base_dir, meeting["date"], local_tz)
        local_date = meeting["date"].astimezone(local_tz)
        date_str = local_date.strftime("%Y-%m-%d %H:%M %Z")
        date_only = local_date.strftime("%Y-%m-%d")
        filename = f"{date_only}_{local_date.strftime('%H%M')}_{create_safe_title(meeting['title'])}.md"

        if file_exists_anywhere(base_dir, filename):
            print(f"  Already exists: {filename}")
            return False

        tags = generate_tags(meeting["title"], classification)
        participant_links = [f"[[{p}]]" for p in meeting["participants"]] if meeting["participants"] else []

        md = "---\n"
        md += f"date: {date_only}\n"
        md += f'time: "{local_date.strftime("%H:%M")}"\n'
        md += "type: meeting\n"
        md += f"{format_tags_inline(tags)}\n"
        if meeting["participants"]:
            md += f"{format_participants_frontmatter(meeting['participants'])}\n"
        md += f"granola_id: {meeting['id']}\n"
        if meeting.get("web_url"):
            md += f"granola_url: {meeting['web_url']}\n"
        if classification == "personal":
            md += "private: true\n"
        md += "---\n\n"

        md += f"# {meeting['title']}\n\n"
        md += f"**Date:** {date_str}\n"
        if participant_links:
            md += f"**Participants:** {', '.join(participant_links)}\n"
        if transcript.get("speakers"):
            md += f"**Speakers:** {', '.join(transcript['speakers'])}\n"
        md += f"\n---\n\n## Transcript\n\n{transcript['content']}\n"
        md += build_related_links(meeting, local_tz, "transcript")

        if DRY_RUN:
            print(f"  [DRY RUN] Would save transcript: {output_path / filename}")
            return True
        output_path.mkdir(parents=True, exist_ok=True)
        with open(output_path / filename, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  ✓ Saved: {filename}")
        return True
    except Exception as e:
        print(f"  ERROR saving transcript {meeting['title']}: {e}")
        return False


def save_notes_to_file(meeting, sections, base_dir, local_tz, classification):
    try:
        output_path = get_output_dir(base_dir, meeting["date"], local_tz)
        local_date = meeting["date"].astimezone(local_tz)
        date_str = local_date.strftime("%Y-%m-%d")
        safe_title = create_safe_title_kebab(meeting["title"])
        filename = f"{date_str}-{safe_title}-notes.md"

        if file_exists_anywhere(base_dir, filename):
            print(f"  Notes exist: {filename}")
            return False

        tags = generate_tags(meeting["title"], classification)
        if "notes" not in tags:
            tags.append("notes")

        sections_md = "\n\n".join(f"## {s['title']}\n\n{s['content']}" for s in sections)

        md = "---\n"
        md += f"date: {date_str}\n"
        md += f'time: "{local_date.strftime("%H:%M")}"\n'
        md += "type: meeting-notes\n"
        md += f"{format_tags_inline(tags)}\n"
        if meeting["participants"]:
            md += f"{format_participants_frontmatter(meeting['participants'])}\n"
        md += f"granola_id: {meeting['id']}\n"
        if meeting.get("web_url"):
            md += f"granola_url: {meeting['web_url']}\n"
        if classification == "personal":
            md += "private: true\n"
        md += "---\n\n"

        md += f"# {meeting['title']}\n\n"
        md += f"**Date:** {date_str} {local_date.strftime('%H:%M %Z')}\n"
        if meeting["participants"]:
            participant_links = [f"[[{p}]]" for p in meeting["participants"]]
            md += f"**Participants:** {', '.join(participant_links)}\n"
        md += f"\n---\n\n{sections_md}\n"
        md += build_related_links(meeting, local_tz, "notes")

        if DRY_RUN:
            print(f"  [DRY RUN] Would save notes: {output_path / filename}")
            return True
        output_path.mkdir(parents=True, exist_ok=True)
        with open(output_path / filename, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  ✓ Saved notes: {filename}")
        return True
    except Exception as e:
        print(f"  ERROR saving notes {meeting['title']}: {e}")
        return False


# ============================================================================
# Main
# ============================================================================

def main():
    if not GRANOLA_API_KEY:
        print("\n=== Granola Backup ===")
        print("ERROR: GRANOLA_API_KEY not set. Add to ~/.env:")
        print("  GRANOLA_API_KEY=grn_yourkey")
        write_status(success=False, error_msg="GRANOLA_API_KEY missing")
        sys.exit(1)

    local_tz = detect_timezone()

    print("\n=== Granola Backup ===" + (" (DRY RUN)" if DRY_RUN else ""))
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Work output: {WORK_OUTPUT_DIR}")
    print(f"Personal output: {PERSONAL_OUTPUT_DIR}")
    print(f"Timezone: {local_tz}")

    prev_status = _read_status() or {}
    updated_after = None if BACKFILL else prev_status.get("last_updated_at")
    if updated_after:
        print(f"Incremental since: {updated_after}")
    else:
        print("Full backfill (no prior sync timestamp)")

    print("\nFetching notes...")
    try:
        summaries = list(list_notes(updated_after=updated_after))
    except GranolaAPIError as e:
        print(f"ERROR: {e}")
        write_status(success=False, error_msg=str(e))
        sys.exit(1)

    print(f"Found: {len(summaries)} note(s) to consider\n")

    transcript_saved = 0
    notes_saved = 0
    max_updated_at = updated_after

    for i, summary in enumerate(summaries, 1):
        title = summary.get("title") or "Untitled Meeting"
        updated_at = summary.get("updated_at")
        if updated_at and (not max_updated_at or updated_at > max_updated_at):
            max_updated_at = updated_at

        try:
            note = get_note(summary["id"], include_transcript=True)
        except GranolaAPIError as e:
            print(f"  [{i}/{len(summaries)}] {title} — fetch failed: {e}")
            continue
        time.sleep(REQUEST_DELAY)

        meeting = note_to_meeting(note)
        cls = classify_meeting(meeting["title"], meeting["participants"])
        out_dir = PERSONAL_OUTPUT_DIR if cls == "personal" else WORK_OUTPUT_DIR

        print(f"[{cls}] {meeting['title']}")

        transcript = note_to_transcript(note)
        if transcript:
            if save_transcript_to_file(meeting, transcript, out_dir, local_tz, cls):
                transcript_saved += 1

        sections = note_to_summary_sections(note)
        if sections:
            if save_notes_to_file(meeting, sections, out_dir, local_tz, cls):
                notes_saved += 1

    total_saved = transcript_saved + notes_saved
    if not DRY_RUN:
        write_status(
            success=True,
            saved_count=total_saved,
            last_updated_at=max_updated_at,
        )

    print(f"\n=== Backup Complete ===")
    print(f"Saved: {transcript_saved} transcript(s), {notes_saved} notes")
    if max_updated_at:
        print(f"Watermark: {max_updated_at}")


if __name__ == "__main__":
    main()
