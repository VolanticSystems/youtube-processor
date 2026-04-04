"""Core video processing engine. Fetches transcript, generates summary and edited transcript."""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
import yt_dlp


def load_config():
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def fmt_ts(total_seconds):
    """Format seconds as M:SS or H:MM:SS for display."""
    total_seconds = int(total_seconds)
    hours = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def extract_video_id(url):
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_video_title(video_id):
    """Fetch video title from YouTube oembed API."""
    try:
        resp = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("title", f"Video {video_id}")
    except Exception:
        pass
    return f"Video {video_id}"


def fetch_transcript(video_id):
    """Fetch transcript segments from YouTube via yt-dlp with Firefox cookie auth."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "json3",
        "cookiesfrombrowser": ("firefox",),
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)

    # Look for English subtitles: prefer manual, fall back to auto-generated
    subs = info.get("subtitles", {}) or {}
    auto = info.get("automatic_captions", {}) or {}
    en_entries = subs.get("en") or auto.get("en") or []

    json3_url = None
    for fmt in en_entries:
        if fmt.get("ext") == "json3":
            json3_url = fmt["url"]
            break

    if not json3_url:
        raise ValueError(f"No English subtitles available for {video_id}")

    resp = requests.get(json3_url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    segments = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue
        text = "".join(seg.get("utf8", "") for seg in event["segs"]).strip()
        if text:
            start_ms = event.get("tStartMs", 0)
            dur_ms = event.get("dDurationMs", 0)
            segments.append({
                "start": start_ms / 1000.0,
                "duration": dur_ms / 1000.0,
                "text": text,
            })

    if not segments:
        raise ValueError(f"Transcript was empty for {video_id}")

    return segments


def fetch_chapters(video_id):
    """Fetch chapter markers from YouTube video metadata via yt-dlp. Returns list of {start, title} or empty list."""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "cookiesfrombrowser": ("firefox",)}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
            chapters = info.get("chapters") or []
            return [{"start": ch["start_time"], "title": ch["title"]} for ch in chapters]
    except Exception:
        return []


def download_video(video_id, output_dir, progress_callback=None):
    """Download video at up to 1080p using yt-dlp. Returns path to video file."""
    output_dir = Path(output_dir)
    video_path = output_dir / "video.mp4"
    if video_path.exists():
        if progress_callback:
            progress_callback("Video already cached.")
        return str(video_path)

    if progress_callback:
        progress_callback("Downloading video...")

    hooks = []
    if progress_callback:
        def hook(d):
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "").strip()
                progress_callback(f"Downloading video... {pct}")
            elif d["status"] == "finished":
                progress_callback("Processing video...")
        hooks.append(hook)

    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "video.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": hooks,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    return str(video_path)


def build_condensed_transcript(segments):
    """Group transcript segments into ~30-second blocks with timestamps."""
    lines = []
    current_line = []
    current_start = segments[0]["start"]

    for seg in segments:
        if seg["start"] - current_start >= 30 and current_line:
            minutes = int(current_start) // 60
            seconds = int(current_start) % 60
            lines.append(f"[{minutes:02d}:{seconds:02d}] {' '.join(current_line)}")
            current_line = []
            current_start = seg["start"]
        current_line.append(seg["text"])

    if current_line:
        minutes = int(current_start) // 60
        seconds = int(current_start) % 60
        lines.append(f"[{minutes:02d}:{seconds:02d}] {' '.join(current_line)}")

    return lines


def fetch_model_pricing(model):
    """Fetch live per-token pricing from OpenRouter for a model. Returns {prompt, completion} per-token rates."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        for m in resp.json().get("data", []):
            if m.get("id") == model:
                p = m.get("pricing", {})
                return {
                    "prompt": float(p.get("prompt", 0)),
                    "completion": float(p.get("completion", 0)),
                }
    except Exception:
        pass
    return None


# Cache pricing per session to avoid hitting the API on every call
_pricing_cache = {}


def get_pricing(model):
    """Get pricing for a model, cached per session."""
    if model not in _pricing_cache:
        _pricing_cache[model] = fetch_model_pricing(model)
    return _pricing_cache[model]


def log_api_call(model, purpose, video_id, usage, pricing):
    """Append a log entry for an API call to api_log.jsonl."""
    config = load_config()
    log_path = Path(config.get("output_dir", "./library")) / "api_log.jsonl"

    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "purpose": purpose,
        "video_id": video_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    if pricing:
        cost = input_tokens * pricing["prompt"] + output_tokens * pricing["completion"]
        entry["pricing"] = {
            "prompt_per_token": pricing["prompt"],
            "completion_per_token": pricing["completion"],
        }
        entry["estimated_cost"] = round(cost, 6)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return entry.get("estimated_cost", 0)


def call_openrouter(model, messages, max_tokens, timeout=300, purpose="", video_id="",
                    progress_callback=None, progress_label=""):
    """Make a streaming OpenRouter API call with live progress and stall detection."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set in environment")

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True},
        timeout=(15, timeout),  # (connect timeout, read timeout)
        stream=True,
    )

    if response.status_code != 200:
        try:
            err = response.json()
        except Exception:
            err = response.text
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {err}")

    content_parts = []
    token_count = 0
    last_token_time = time.time()
    stall_limit = 60  # seconds with no tokens before we consider it dead

    label = progress_label or purpose or "API call"

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data: "):
            payload = line[6:]
            if payload.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                if "error" in chunk:
                    raise RuntimeError(f"OpenRouter stream error: {chunk['error']}")
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                text = delta.get("content", "")
                if text:
                    content_parts.append(text)
                    token_count += len(text.split())  # rough word-based estimate
                    last_token_time = time.time()
                    if progress_callback and token_count % 20 == 0:
                        progress_callback(f"{label} ({token_count:,} tokens)")
            except json.JSONDecodeError:
                continue

        # Check for stall
        if time.time() - last_token_time > stall_limit and token_count > 0:
            raise RuntimeError(
                f"API stopped responding after {token_count:,} tokens "
                f"({int(time.time() - last_token_time)}s since last token)"
            )

    content = "".join(content_parts)
    if not content.strip():
        raise RuntimeError(f"OpenRouter returned empty response for {purpose}")

    if progress_callback:
        progress_callback(f"{label} (complete, ~{token_count:,} tokens)")

    # Build usage dict from token count estimate (streaming doesn't always return usage)
    # Use rough 0.75 words-per-token ratio to estimate
    est_output_tokens = int(token_count / 0.75)
    prompt_text = "".join(m.get("content", "") for m in messages)
    est_input_tokens = int(len(prompt_text.split()) / 0.75)
    usage = {"prompt_tokens": est_input_tokens, "completion_tokens": est_output_tokens}

    # Log with live pricing
    pricing = get_pricing(model)
    log_api_call(model, purpose, video_id, usage, pricing)

    return content, usage


def generate_summary(video_id, transcript_lines, config, progress_callback=None, context_hint="", chapters=None,
                     fn_transcript_edited="transcript_edited.html", fn_transcript_full="transcript_full.html"):
    """Generate the hierarchical HTML summary."""
    transcript_text = "\n".join(transcript_lines)

    hint_block = f"\nCONTEXT NOTE: {context_hint}\nUse this context to better interpret the transcript — fix misheard words, preserve domain-specific terms, etc.\n" if context_hint else ""

    chapters_block = ""
    if chapters:
        chapter_lines = [f"  [{fmt_ts(int(ch['start']))}] {ch['title']}" for ch in chapters]
        chapters_block = "\n\nVIDEO CHAPTERS (from the video author — use these as your section structure):\n" + "\n".join(chapter_lines) + "\n\nIMPORTANT: Use the original chapter titles as your <h2> headings EXACTLY as written. You may group adjacent short chapters into one section where it makes sense, but ALWAYS keep the original chapter title as the primary heading. If you want to add your own descriptive subtitle, put it AFTER the timestamp in a <span> tag like this:\n<h2>Original Chapter Title <a href=\"...\">[M:SS]</a> <span style=\"font-weight: normal; font-style: italic; font-size: 0.75em; color: #666;\">— your brief descriptive note</span></h2>\n"

    prompt = f"""You are summarizing a YouTube video (ID: {video_id}).
{hint_block}{chapters_block}

Below is the timestamped transcript. Your job is to produce a HIERARCHICAL HTML summary with these requirements:

1. **Overall summary first**: Start with an <h1> title, then immediately provide a 2-3 paragraph overview of the ENTIRE conversation — what it's about, who's talking, the major themes, and why it matters. This should give the reader a complete picture before they dive into sections.

2. **Top-level sections**: Identify the major topics/themes discussed. For a 2+ hour video, expect 10-20 sections covering the ENTIRE video from start to finish. Each gets an <h2> heading with the timestamp AFTER the title, like: <h2>Topic Name <a href="...">[M:SS or H:MM:SS]</a></h2>

3. **Sub-points**: Under each topic, provide 2-5 bullet points summarizing the key insights, claims, or arguments made.

4. **Timestamp links**: Each sub-point should include a clickable timestamp link. Format: <a href="https://www.youtube.com/watch?v={video_id}&t=XXXs" target="yt-player">[M:SS or H:MM:SS]</a> where XXX is the seconds value.

5. **Two expandable transcript dropdowns** after each section's bullet points, in this exact order:
   a. FIRST: <details><summary>Transcript excerpt - edited</summary> — a cleaned-up, readable version of the relevant transcript for this section, with proper sentences and paragraphs. At the end of this block, add: <p><a href="{fn_transcript_edited}#tXXX">Read full edited transcript at this point &rarr;</a></p> where XXX is the seconds value.
   b. SECOND: <details><summary>Transcript excerpt - raw</summary> — the verbatim captions. At the end of this block, add: <p><a href="{fn_transcript_full}#tXXX">Read full raw transcript at this point &rarr;</a></p> where XXX is the seconds value.

6. Output ONLY the HTML body content (no <html>, <head>, or <body> tags).

7. Keep the summary insightful and opinionated — highlight what's surprising, controversial, or especially useful. Don't just list topics blandly.

8. CRITICAL: You MUST cover the ENTIRE video from beginning to end. Do not stop partway through. Every major topic shift should get its own section.

TRANSCRIPT:
{transcript_text}"""

    content, usage = call_openrouter(
        config["model"],
        [{"role": "user", "content": prompt}],
        config["max_summary_tokens"],
        purpose="summary",
        video_id=video_id,
        progress_callback=progress_callback,
        progress_label="Generating summary",
    )

    # Fix timestamps: convert [0X:MM:SS] to [X:MM:SS]
    content = re.sub(r'\[0(\d:\d{2}:\d{2})\]', r'[\1]', content)

    return content, usage


def generate_raw_transcript_html(video_id, segments, chapters=None):
    """Generate the raw transcript HTML page with optional chapter headers."""
    lines = []
    current_block = []
    current_start = segments[0]["start"]
    chapter_idx = 0
    chapters = chapters or []

    def flush_block():
        """Flush the current block as a <p> element."""
        if not current_block:
            return
        secs = int(current_start)
        ts_display = fmt_ts(secs)
        yt_link = f"https://www.youtube.com/watch?v={video_id}&t={secs}s"
        text = " ".join(current_block)
        lines.append(
            f'<p id="t{secs}"><a href="{yt_link}" target="yt-player" class="ts">[{ts_display}]</a> {text}</p>'
        )

    def insert_chapters_before(timestamp):
        """Insert any chapter headers that fall before the given timestamp."""
        nonlocal chapter_idx
        while chapter_idx < len(chapters) and chapters[chapter_idx]["start"] <= timestamp:
            ch = chapters[chapter_idx]
            ch_secs = int(ch["start"])
            ch_ts = fmt_ts(ch_secs)
            ch_link = f"https://www.youtube.com/watch?v={video_id}&t={ch_secs}s"
            lines.append(
                f'<h3 class="chapter"><a href="{ch_link}" target="yt-player">[{ch_ts}]</a> {ch["title"]}</h3>'
            )
            chapter_idx += 1

    for seg in segments:
        if seg["start"] - current_start >= 30 and current_block:
            flush_block()
            current_block = []
            current_start = seg["start"]
            insert_chapters_before(current_start)
        current_block.append(seg["text"])

    flush_block()
    # Insert any remaining chapters at the end
    insert_chapters_before(float("inf"))

    return "\n".join(lines)


def generate_edited_transcript(video_id, segments, config, progress_callback=None, context_hint="", step_state=None):
    """Generate the edited (cleaned up) transcript via LLM."""
    chunk_minutes = config.get("chunk_minutes", 10)

    # Group segments into chunks
    chunks = []
    current_chunk = []
    chunk_start = segments[0]["start"]

    for seg in segments:
        if seg["start"] - chunk_start >= chunk_minutes * 60 and current_chunk:
            chunks.append({"start": chunk_start, "segments": current_chunk})
            current_chunk = []
            chunk_start = seg["start"]
        current_chunk.append(seg)

    if current_chunk:
        chunks.append({"start": chunk_start, "segments": current_chunk})

    all_blocks = []
    total_input = 0
    total_output = 0

    for i, chunk in enumerate(chunks):
        # Build timestamped raw text for this chunk
        lines = []
        current_block = []
        block_start = chunk["segments"][0]["start"]

        for seg in chunk["segments"]:
            if seg["start"] - block_start >= 30 and current_block:
                secs = int(block_start)
                m, s = secs // 60, secs % 60
                lines.append(f"[{m:02d}:{s:02d}] {' '.join(current_block)}")
                current_block = []
                block_start = seg["start"]
            current_block.append(seg["text"])

        if current_block:
            secs = int(block_start)
            m, s = secs // 60, secs % 60
            lines.append(f"[{m:02d}:{s:02d}] {' '.join(current_block)}")

        raw_text = "\n".join(lines)

        # Build explicit anchor IDs
        anchor_list = []
        for line in lines:
            ts_match = line.split("]")[0].lstrip("[")
            parts = ts_match.split(":")
            if len(parts) == 2:
                total_secs = int(parts[0]) * 60 + int(parts[1])
                anchor_list.append(f"t{total_secs}")
        anchor_ids = ", ".join(anchor_list)

        hint_block = f"\nCONTEXT NOTE: {context_hint}\nUse this context to better interpret the transcript — fix misheard words, preserve domain-specific terms, etc.\n" if context_hint else ""

        prompt = f"""You are editing a raw YouTube transcript into clean, readable prose.
{hint_block}
RULES:
1. Preserve ALL the content and meaning — do not summarize or skip anything.
2. Fix sentence structure, grammar, and punctuation. Break into proper paragraphs.
3. Remove filler words (um, uh, like, you know) and false starts, but keep the speaker's voice and style.
4. When the speaker changes (interviewer vs guest), start a new paragraph.
5. Insert timestamp anchors using EXACTLY these IDs at the corresponding points in the text: {anchor_ids}
   Format: <span id="t{{SECONDS}}" class="ts-anchor"></span> placed at the START of the paragraph closest to that timestamp. Use every single ID listed — do not skip any.
6. Output ONLY the cleaned HTML paragraphs — no wrapping tags, no headers, no explanation.
7. This is chunk {i + 1} of {len(chunks)} — just process this portion.

RAW TRANSCRIPT:
{raw_text}"""

        if step_state is not None:
            step_state[0] = 2 + i  # step 1 was summary, chunks start at 2

        content, usage = call_openrouter(
            config["model"],
            [{"role": "user", "content": prompt}],
            config.get("max_clean_tokens", 6000),
            timeout=180,
            purpose=f"edited_transcript_chunk_{i+1}",
            video_id=video_id,
            progress_callback=progress_callback,
            progress_label=f"Editing transcript {i+1}/{len(chunks)}",
        )

        total_input += usage.get("prompt_tokens", 0)
        total_output += usage.get("completion_tokens", 0)

        # Post-process: extract text, rebuild as clean <p id="tXXX"> blocks
        all_blocks.append((chunk["start"], anchor_list, content))

        if i < len(chunks) - 1:
            time.sleep(1)

    # Post-process all blocks into clean structure matching raw transcript format
    final_paragraphs = []
    for chunk_start_time, anchor_list, content in all_blocks:
        # Split on span anchors
        parts = re.split(r'(<span id="t\d+" class="ts-anchor"></span>)', content)
        current_id = None

        for part in parts:
            span_match = re.match(r'<span id="(t\d+)" class="ts-anchor"></span>', part)
            if span_match:
                current_id = span_match.group(1)
            elif current_id:
                text = part.strip()
                text = re.sub(r"</?p[^>]*>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    secs = int(current_id[1:])
                    ts_display = fmt_ts(secs)
                    yt_link = f"https://www.youtube.com/watch?v={video_id}&t={secs}s"
                    final_paragraphs.append(
                        f'<p id="{current_id}"><a href="{yt_link}" target="yt-player" class="ts">[{ts_display}]</a> {text}</p>'
                    )
                current_id = None

    edited_usage = {"prompt_tokens": total_input, "completion_tokens": total_output}
    return final_paragraphs, edited_usage


# ============================================================
# Chapter-based processing (used when video has chapters)
# ============================================================

def group_chapters(chapters, total_duration, target_minutes=8):
    """Group chapters into batches targeting ~target_minutes per batch."""
    if not chapters:
        return []

    groups = []
    current_group = []
    group_start = chapters[0]["start"]

    for i, ch in enumerate(chapters):
        ch_end = chapters[i + 1]["start"] if i + 1 < len(chapters) else total_duration
        ch_info = {"start": ch["start"], "title": ch["title"], "end": ch_end}

        if current_group and (ch_end - group_start) > target_minutes * 60:
            groups.append(current_group)
            current_group = [ch_info]
            group_start = ch["start"]
        else:
            current_group.append(ch_info)

    if current_group:
        groups.append(current_group)

    return groups


def process_chapter_group(video_id, group, segments, config, group_num, total_groups, progress_callback=None, context_hint=""):
    """Process a chapter group: produces edited transcript + bullet summaries per chapter."""
    group_start = group[0]["start"]
    group_end = group[-1]["end"]
    group_segments = [s for s in segments if s["start"] >= group_start and s["start"] < group_end]

    if not group_segments:
        return [], {"prompt_tokens": 0, "completion_tokens": 0}

    # Build 30-second text blocks with timestamps
    lines = []
    current_block = []
    block_start = group_segments[0]["start"]

    for seg in group_segments:
        if seg["start"] - block_start >= 30 and current_block:
            secs = int(block_start)
            ts_display = fmt_ts(secs)
            lines.append(f"[{ts_display}] {' '.join(current_block)}")
            current_block = []
            block_start = seg["start"]
        current_block.append(seg["text"])

    if current_block:
        secs = int(block_start)
        ts_display = fmt_ts(secs)
        lines.append(f"[{ts_display}] {' '.join(current_block)}")

    raw_text = "\n".join(lines)

    # Build anchor IDs
    anchor_list = []
    for line in lines:
        ts_match = line.split("]")[0].lstrip("[")
        parts = ts_match.split(":")
        if len(parts) == 2:
            total_secs = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            total_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            continue
        anchor_list.append(f"t{total_secs}")
    anchor_ids = ", ".join(anchor_list)

    # Chapter listing
    chapter_listing = "\n".join(
        f'  Chapter: "{ch["title"]}" [{fmt_ts(int(ch["start"]))}]'
        for ch in group
    )

    hint_block = f"\nCONTEXT NOTE: {context_hint}\nUse this to interpret accents, jargon, or domain terms.\n" if context_hint else ""

    prompt = f"""You are editing a raw YouTube transcript and creating chapter summaries for video {video_id}.
{hint_block}
This chunk contains {len(group)} chapter(s):
{chapter_listing}

For EACH chapter, provide:
1. A brief summary (3-5 bullet points as HTML <ul><li> list)
2. The cleaned-up transcript as readable prose paragraphs

Format your output EXACTLY like this for EACH chapter:

<!-- CHAPTER: exact chapter title here -->
<!-- SUMMARY -->
<ul>
<li>key point</li>
</ul>
<!-- /SUMMARY -->
<!-- TRANSCRIPT -->
cleaned prose paragraphs with timestamp anchors
<!-- /TRANSCRIPT -->

RULES for transcript editing:
1. Preserve ALL content — do not summarize or skip anything in the TRANSCRIPT section.
2. Fix grammar, punctuation, sentence structure. Break into proper paragraphs.
3. Remove filler words (um, uh, like, you know) and false starts, keep the speaker's voice.
4. When the speaker changes, start a new paragraph.
5. Insert timestamp anchors using EXACTLY these IDs at corresponding points: {anchor_ids}
   Format: <span id="t{{SECONDS}}" class="ts-anchor"></span> at the START of the nearest paragraph.
6. Assign each anchor to the correct chapter based on its timestamp.
7. This is group {group_num} of {total_groups}.

RAW TRANSCRIPT:
{raw_text}"""

    content, usage = call_openrouter(
        config["model"],
        [{"role": "user", "content": prompt}],
        config.get("max_clean_tokens", 6000) + len(group) * 500,
        timeout=180,
        purpose=f"chapter_group_{group_num}",
        video_id=video_id,
        progress_callback=progress_callback,
        progress_label=f"Group {group_num}/{total_groups}",
    )

    chapter_results = parse_chapter_group_output(content, group, video_id)

    return chapter_results, usage


def parse_chapter_group_output(content, group, video_id):
    """Parse AI output into per-chapter summaries and edited transcript blocks."""
    parts = re.split(r'<!-- CHAPTER:\s*(.+?)\s*-->', content)

    results = []
    for i in range(1, len(parts), 2):
        block = parts[i + 1] if i + 1 < len(parts) else ""

        # Extract summary
        summary_match = re.search(r'<!-- SUMMARY -->(.*?)<!-- /SUMMARY -->', block, re.DOTALL)
        if summary_match:
            summary_html = summary_match.group(1).strip()
        else:
            ul_match = re.search(r'(<ul>.*?</ul>)', block, re.DOTALL)
            summary_html = ul_match.group(1).strip() if ul_match else ""

        # Extract transcript
        transcript_match = re.search(r'<!-- TRANSCRIPT -->(.*?)(?:<!-- /TRANSCRIPT -->|$)', block, re.DOTALL)
        if transcript_match:
            transcript_raw = transcript_match.group(1).strip()
        else:
            after_summary = re.split(r'<!-- /SUMMARY -->', block, 1)
            transcript_raw = after_summary[1].strip() if len(after_summary) > 1 else block.strip()

        # Post-process transcript into clean <p id="tXXX"> blocks
        edited_paragraphs = []
        t_parts = re.split(r'(<span id="t\d+" class="ts-anchor"></span>)', transcript_raw)
        current_id = None

        for part in t_parts:
            span_match = re.match(r'<span id="(t\d+)" class="ts-anchor"></span>', part)
            if span_match:
                current_id = span_match.group(1)
            elif current_id:
                text = part.strip()
                text = re.sub(r"</?p[^>]*>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    secs = int(current_id[1:])
                    ts_display = fmt_ts(secs)
                    yt_link = f"https://www.youtube.com/watch?v={video_id}&t={secs}s"
                    edited_paragraphs.append(
                        f'<p id="{current_id}"><a href="{yt_link}" target="yt-player" class="ts">[{ts_display}]</a> {text}</p>'
                    )
                current_id = None

        results.append({
            "summary": summary_html,
            "edited_paragraphs": edited_paragraphs,
        })

    # Trim extra results if AI produced more markers than chapters
    results = results[:len(group)]

    # Match metadata from group
    for j, ch in enumerate(group):
        if j < len(results):
            results[j]["title"] = ch["title"]
            results[j]["start"] = ch["start"]
            results[j]["end"] = ch["end"]
        else:
            results.append({
                "title": ch["title"],
                "start": ch["start"],
                "end": ch["end"],
                "summary": "",
                "edited_paragraphs": [],
            })

    return results


def assemble_summary(video_id, all_chapter_data, config, progress_callback=None, context_hint=""):
    """Lightweight summary assembly from pre-generated chapter summaries."""

    hint_block = f"\nCONTEXT NOTE: {context_hint}\n" if context_hint else ""

    chapter_lines = []
    for ch in all_chapter_data:
        ts = fmt_ts(int(ch["start"]))
        summary_text = re.sub(r'</?[^>]+>', '', ch.get("summary", "")).strip()
        chapter_lines.append(f'[{ts}] {ch["title"]}\n{summary_text}')

    chapters_input = "\n\n".join(chapter_lines)

    prompt = f"""You are assembling a summary for YouTube video {video_id} from pre-generated chapter summaries.
{hint_block}
CHAPTER SUMMARIES:
{chapters_input}

REQUIREMENTS:
1. Start with an <h1> title (your own descriptive title for the video).
2. Write 2-3 paragraphs providing an overview of the ENTIRE conversation.
3. For each chapter (or group of closely related chapters), create an <h2> section.
4. Use the ORIGINAL chapter title EXACTLY as written in the <h2>.
5. Format: <h2>Original Title <a href="https://www.youtube.com/watch?v={video_id}&t=XXXs" target="yt-player">[timestamp]</a> <span style="font-weight: normal; font-style: italic; font-size: 0.75em; color: #666;">— your brief note</span></h2>
6. Under each <h2>, refine the provided bullet points into 3-5 <ul><li> items with timestamp links.
7. You may group very small adjacent chapters, but keep original titles visible.
8. Be insightful and opinionated — highlight surprises, controversies, key insights.
9. Do NOT include transcript dropdowns — they will be added automatically.
10. Output ONLY HTML body content (no <html>, <head>, <body> tags).

CRITICAL: Cover ALL chapters from beginning to end. Do not stop early."""

    content, usage = call_openrouter(
        config["model"],
        [{"role": "user", "content": prompt}],
        16000,
        purpose="summary_assembly",
        video_id=video_id,
        progress_callback=progress_callback,
        progress_label="Assembling summary",
    )

    content = re.sub(r'\[0(\d:\d{2}:\d{2})\]', r'[\1]', content)

    return content, usage


def insert_transcript_dropdowns(summary_content, all_chapter_data, segments, video_id, safe_title=""):
    """Insert transcript excerpt dropdowns into the summary after each section."""
    fn_edited = content_filename("transcript_edited", safe_title, "html") if safe_title else "transcript_edited.html"
    fn_full = content_filename("transcript_full", safe_title, "html") if safe_title else "transcript_full.html"

    # Build dropdown HTML keyed by chapter start time
    dropdowns = {}
    for ch in all_chapter_data:
        secs = int(ch["start"])

        # Edited excerpt: first 3 paragraphs
        edited_paras = ch.get("edited_paragraphs", [])
        edited_excerpt = "\n".join(edited_paras[:3]) if edited_paras else ""

        # Raw excerpt: ~90 seconds of raw transcript
        ch_end = ch.get("end", secs + 90)
        raw_segs = [s for s in segments if s["start"] >= secs and s["start"] < min(secs + 90, ch_end)]
        raw_text = " ".join(s["text"] for s in raw_segs)
        if len(raw_text) > 500:
            raw_text = raw_text[:497] + "..."

        dropdowns[secs] = f"""<details>
<summary>Transcript excerpt - edited</summary>
{edited_excerpt}
<p><a href="{fn_edited}#t{secs}">Read full edited transcript at this point &rarr;</a></p>
</details>
<details>
<summary>Transcript excerpt - raw</summary>
<p>{raw_text}</p>
<p><a href="{fn_full}#t{secs}">Read full raw transcript at this point &rarr;</a></p>
</details>"""

    # Split summary into sections by <h2> tags
    sections = re.split(r'(?=<h2>)', summary_content)

    # Extract timestamp from each section's h2
    section_timestamps = []
    for section in sections:
        if section.strip().startswith('<h2>'):
            ts_match = re.search(r't=(\d+)s', section)
            section_timestamps.append(int(ts_match.group(1)) if ts_match else None)
        else:
            section_timestamps.append(None)

    result = []
    for i, section in enumerate(sections):
        result.append(section)

        if section_timestamps[i] is not None:
            current_ts = section_timestamps[i]
            next_ts = float('inf')
            for j in range(i + 1, len(sections)):
                if section_timestamps[j] is not None:
                    next_ts = section_timestamps[j]
                    break

            for ch_ts in sorted(dropdowns.keys()):
                if ch_ts >= current_ts - 30 and ch_ts < next_ts:
                    result.append(dropdowns[ch_ts])

    return '\n'.join(result)


def build_edited_transcript_body(all_chapter_data, video_id):
    """Build edited transcript HTML body from per-chapter data."""
    parts = []
    for ch in all_chapter_data:
        secs = int(ch["start"])
        ts_display = fmt_ts(secs)
        yt_link = f"https://www.youtube.com/watch?v={video_id}&t={secs}s"
        parts.append(
            f'<h3 class="chapter"><a href="{yt_link}" target="yt-player">[{ts_display}]</a> {ch["title"]}</h3>'
        )
        parts.extend(ch.get("edited_paragraphs", []))
    return "\n".join(parts)


# HTML templates shared across pages
YT_REUSE_JS = """
<script>
var playerWindow = null;
document.addEventListener('click', function(e) {
  var link = e.target.closest('a[target="yt-player"]');
  if (!link) return;
  e.preventDefault();
  var url = new URL(link.href);
  var videoId = url.searchParams.get('v');
  var t = (url.searchParams.get('t') || '0').replace('s', '');
  var playerUrl = '/player/' + videoId + '#t=' + t;
  try {
    if (playerWindow && !playerWindow.closed) {
      if (typeof playerWindow.seekTo === 'function') {
        playerWindow.seekTo(parseInt(t));
      } else {
        playerWindow.location.href = playerUrl;
      }
      playerWindow.focus();
      return;
    }
  } catch(ex) {}
  playerWindow = window.open(playerUrl, 'video-player');
  if (playerWindow) playerWindow.focus();
});
</script>
"""

NEAREST_ANCHOR_JS = """
<script>
window.addEventListener('load', function() {
  if (!window.location.hash) return;
  var target = document.getElementById(window.location.hash.substring(1));
  if (target) {
    target.classList.add('landed-here');
    target.scrollIntoView();
    return;
  }
  var match = window.location.hash.match(/^#t(\\d+)$/);
  if (!match) return;
  var targetTime = parseInt(match[1]);
  var anchors = document.querySelectorAll('[id^="t"]');
  var nearest = null, nearestDiff = Infinity;
  anchors.forEach(function(el) {
    var m = el.id.match(/^t(\\d+)$/);
    if (m) {
      var diff = Math.abs(parseInt(m[1]) - targetTime);
      if (diff < nearestDiff) { nearestDiff = diff; nearest = el; }
    }
  });
  if (nearest) {
    nearest.classList.add('landed-here');
    nearest.scrollIntoView();
  }
});
</script>
"""

TRANSCRIPT_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 800px; margin: 40px auto; padding: 0 40px; line-height: 1.8; color: #333; }
  h1 { color: #1a1a1a; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }
  p { margin: 14px 0; }
  .ts { color: #2980b9; text-decoration: none; font-weight: 600; font-family: monospace; margin-right: 6px; }
  .ts:hover { text-decoration: underline; }
  .landed-here { border-left: 4px solid #2980b9; padding: 12px 16px; margin-left: -20px; background: linear-gradient(to right, #e8f4fd 0%, transparent 40%); border-radius: 4px; }
  .landed-here::before { content: "\\25B6  You are here"; display: block; color: #2980b9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 0.8em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .chapter { color: #2c3e50; margin-top: 36px; margin-bottom: 12px; padding-bottom: 6px; border-bottom: 2px solid #2980b9; font-size: 1.15em; }
  .chapter a { color: #2980b9; text-decoration: none; font-family: monospace; font-size: 0.85em; margin-right: 6px; }
  .chapter a:hover { text-decoration: underline; }
  .back-link { position: fixed; right: 20px; top: 20px; background: #e8f4fd; padding: 8px 16px; border-radius: 8px; z-index: 100; box-shadow: 0 2px 6px rgba(0,0,0,0.15); }
  .back-link a { color: #1976d2; text-decoration: none; font-weight: 600; font-size: 0.9em; }
"""

SUMMARY_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 0 40px; line-height: 1.6; color: #333; }
  h1 { color: #1a1a1a; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }
  h2 { color: #2c3e50; margin-top: 30px; }
  h2 a { font-size: 0.75em; font-weight: normal; }
  a { color: #2980b9; text-decoration: none; }
  a:hover { text-decoration: underline; }
  details { background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 6px; padding: 10px 15px; margin: 10px 0; }
  summary { cursor: pointer; font-weight: 600; color: #495057; }
  ul { padding-left: 20px; }
  li { margin-bottom: 8px; }
  .meta-badge { background: #e8f4fd; color: #1976d2; padding: 4px 12px; border-radius: 12px; font-size: 0.85em; display: inline-block; margin-bottom: 10px; margin-right: 8px; }
  .back-link { display: inline-block; margin-bottom: 20px; }
  .back-link a { color: #1976d2; text-decoration: none; font-weight: 600; }
"""


def wrap_summary_html(video_title, summary_content, video_id, duration_str, cost_str):
    """Wrap summary content in a full HTML page."""
    badge_html = f"""<div>
  <span class="meta-badge">Duration: {duration_str}</span>
  <span class="meta-badge">Cost: {cost_str}</span>
</div>"""
    # Insert badges after the first </h1> so they appear below the title
    summary_content = summary_content.replace("</h1>", f"</h1>\n{badge_html}", 1)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{video_title}</title>
<style>{SUMMARY_CSS}</style>
{YT_REUSE_JS}
</head>
<body>
<div class="back-link"><a href="/"> &larr; Back to Library</a></div>
{summary_content}
</body>
</html>"""


def wrap_transcript_html(title, body_content, subtitle=""):
    """Wrap transcript content in a full HTML page."""
    sub_html = f'<p style="color: #666; font-style: italic; font-size: 0.95em;">{subtitle}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{TRANSCRIPT_CSS}</style>
{NEAREST_ANCHOR_JS}
{YT_REUSE_JS}
</head>
<body>
<div class="back-link"><a href="javascript:history.back()">&larr; Back to Summary</a></div>
<h1>{title}</h1>
{sub_html}
{body_content}
</body>
</html>"""


def sanitize_folder_name(name):
    """Remove characters illegal in Windows folder names and trim length."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip('. ')
    if len(name) > 80:
        name = name[:80].rstrip('. ')
    return name or "Untitled"


def content_filename(file_type, safe_title, ext):
    """Build a content filename like 'summary - My Video Title.html'."""
    return f"{file_type} - {safe_title}.{ext}"


def make_unique_dir(parent, name):
    """Create parent/name, appending (1), (2), etc. if it already exists. Returns the Path."""
    candidate = parent / name
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    counter = 1
    while True:
        candidate = parent / f"{name} ({counter})"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        counter += 1


def process_video(video_id, progress_callback=None, context_hint="", category=""):
    """Full pipeline: fetch transcript, generate all outputs, save to library."""
    config = load_config()
    library_dir = Path(config.get("output_dir", "./library"))

    # Fetch video title early so we can use it for the folder name
    if progress_callback:
        progress_callback("Fetching video info...")
    title = fetch_video_title(video_id)

    # Build output path: library/category/sanitized_title/
    cat_dir = library_dir / (category if category else "Uncategorized")
    safe_title = sanitize_folder_name(title)
    output_dir = make_unique_dir(cat_dir, safe_title)

    # Fetch transcript
    if progress_callback:
        progress_callback("Fetching transcript...")
    segments = fetch_transcript(video_id)
    duration_secs = int(segments[-1]["start"] + segments[-1]["duration"])
    duration_str = fmt_ts(duration_secs)

    # Fetch chapters
    if progress_callback:
        progress_callback("Fetching chapters...")
    chapters = fetch_chapters(video_id)

    # Build filenames from sanitized title
    fn_transcript = content_filename("transcript", safe_title, "json")
    fn_transcript_full = content_filename("transcript_full", safe_title, "html")
    fn_transcript_edited = content_filename("transcript_edited", safe_title, "html")
    fn_summary = content_filename("summary", safe_title, "html")

    # Save raw segments
    with open(output_dir / fn_transcript, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    # Generate raw transcript HTML (same for both paths)
    if progress_callback:
        progress_callback("Building raw transcript...")
    raw_body = generate_raw_transcript_html(video_id, segments, chapters)
    raw_html = wrap_transcript_html("Full Transcript", raw_body)
    with open(output_dir / fn_transcript_full, "w", encoding="utf-8") as f:
        f.write(raw_html)

    total_input = 0
    total_output = 0

    # Calculate total API steps for progress display
    if chapters:
        groups = group_chapters(chapters, duration_secs)
        total_steps = len(groups) + 1  # chapter groups + summary assembly
    else:
        chunk_minutes = config.get("chunk_minutes", 10)
        n_chunks = max(1, int(duration_secs / (chunk_minutes * 60)) + (1 if duration_secs % (chunk_minutes * 60) else 0))
        total_steps = 1 + n_chunks  # summary + transcript chunks

    step_state = [0, total_steps]  # [current, total]

    def step_callback(msg):
        """Progress callback that prepends Step N/M."""
        if progress_callback:
            progress_callback(f"Step {step_state[0]}/{step_state[1]}: {msg}")

    if chapters:
        # ===== CHAPTER-BASED FLOW =====
        if progress_callback:
            progress_callback(f"Processing {len(chapters)} chapters in {len(groups)} groups...")

        # Process each chapter group (edited transcript + bullet summaries)
        all_chapter_data = []
        for i, group in enumerate(groups):
            step_state[0] = i + 1
            chapter_results, usage = process_chapter_group(
                video_id, group, segments, config, i + 1, len(groups),
                step_callback, context_hint
            )
            all_chapter_data.extend(chapter_results)
            total_input += usage.get("prompt_tokens", 0)
            total_output += usage.get("completion_tokens", 0)

            if i < len(groups) - 1:
                time.sleep(1)

        # Build edited transcript from chapter data
        edited_body = build_edited_transcript_body(all_chapter_data, video_id)
        edited_html = wrap_transcript_html(
            "Edited Transcript",
            edited_body,
            "This transcript has been cleaned up for readability. Filler words removed, sentences restructured, paragraphs added. Content is preserved faithfully. Click any timestamp to watch that moment on YouTube.",
        )
        with open(output_dir / fn_transcript_edited, "w", encoding="utf-8") as f:
            f.write(edited_html)

        # Assemble summary from chapter summaries (lightweight call)
        step_state[0] = len(groups) + 1
        summary_content, summary_usage = assemble_summary(
            video_id, all_chapter_data, config, step_callback, context_hint
        )
        total_input += summary_usage.get("prompt_tokens", 0)
        total_output += summary_usage.get("completion_tokens", 0)

        # Insert transcript excerpt dropdowns into summary
        summary_content = insert_transcript_dropdowns(
            summary_content, all_chapter_data, segments, video_id, safe_title
        )

    else:
        # ===== NO-CHAPTERS FLOW (original) =====
        transcript_lines = build_condensed_transcript(segments)

        step_state[0] = 1
        summary_content, summary_usage = generate_summary(
            video_id, transcript_lines, config, step_callback, context_hint,
            fn_transcript_edited=fn_transcript_edited, fn_transcript_full=fn_transcript_full,
        )
        total_input += summary_usage.get("prompt_tokens", 0)
        total_output += summary_usage.get("completion_tokens", 0)

        edited_paragraphs, edited_usage = generate_edited_transcript(
            video_id, segments, config, step_callback, context_hint, step_state=step_state
        )
        total_input += edited_usage.get("prompt_tokens", 0)
        total_output += edited_usage.get("completion_tokens", 0)

        # Add 10-minute dividers
        edited_parts = []
        last_10min = -1
        for para in edited_paragraphs:
            id_match = re.search(r'id="t(\d+)"', para)
            if id_match:
                secs = int(id_match.group(1))
                current_10min = secs // 600
                if current_10min > last_10min and secs > 0:
                    last_10min = current_10min
                    edited_parts.append('<hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0;">')
            edited_parts.append(para)

        edited_body = "\n".join(edited_parts)
        edited_html = wrap_transcript_html(
            "Edited Transcript",
            edited_body,
            "This transcript has been cleaned up for readability. Filler words removed, sentences restructured, paragraphs added. Content is preserved faithfully. Click any timestamp to watch that moment on YouTube.",
        )
        with open(output_dir / fn_transcript_edited, "w", encoding="utf-8") as f:
            f.write(edited_html)

    # Calculate cost using live pricing
    pricing = get_pricing(config["model"])
    if pricing:
        est_cost = total_input * pricing["prompt"] + total_output * pricing["completion"]
    else:
        est_cost = 0
    cost_str = f"~${est_cost:.3f}"

    # Save summary HTML
    summary_html = wrap_summary_html(title, summary_content, video_id, duration_str, cost_str)
    with open(output_dir / fn_summary, "w", encoding="utf-8") as f:
        f.write(summary_html)

    # Save metadata
    meta = {
        "video_id": video_id,
        "title": title,
        "safe_title": safe_title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "duration_seconds": duration_secs,
        "duration_display": duration_str,
        "processed_at": datetime.now().isoformat(),
        "last_accessed": datetime.now().isoformat(),
        "model": config["model"],
        "tokens": {
            "total_input": total_input,
            "total_output": total_output,
        },
        "estimated_cost": cost_str,
        "transcript_segments": len(segments),
        "chapters": chapters,
        "context_hint": context_hint,
        "files": {
            "summary": fn_summary,
            "transcript": fn_transcript,
            "transcript_full": fn_transcript_full,
            "transcript_edited": fn_transcript_edited,
        },
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # Download video (non-critical — don't fail the whole process)
    try:
        download_video(video_id, output_dir, progress_callback)
    except Exception as e:
        if progress_callback:
            progress_callback(f"Video download failed (will retry on demand): {e}")

    if progress_callback:
        progress_callback("Done!")

    return meta
