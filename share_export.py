"""Rewrite a summary HTML file into a self-contained sharable copy.

Downstream: /library/<video_id>/download endpoint calls make_shareable_summary
and returns the resulting HTML with a Content-Disposition attachment header.
"""

import re


_LOCAL_ID_RE = re.compile(r"^local_")


def _is_local(video_id):
    return bool(_LOCAL_ID_RE.match(video_id))


def _remove_script_block(html):
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def _strip_yt_player_target(html):
    return re.sub(r'\s+target="yt-player"', "", html, flags=re.IGNORECASE)


def _neutralize_transcript_excerpt_links(html, video_id):
    # Any <a href="transcript_edited - X.html#tN"> or transcript_full points to a
    # neighboring file the recipient does not have. Rewrite to plain text.
    def _repl(m):
        return m.group("label")
    pattern = re.compile(
        r'<a\s+href="transcript_(?:edited|full)\s+-\s+[^"]+"[^>]*>(?P<label>[^<]*)</a>',
        flags=re.IGNORECASE,
    )
    return pattern.sub(_repl, html)


def _rewrite_watch_locally_badge(html, video_id):
    # Remove the local player badge; the recipient has no Flask server.
    return re.sub(
        r'\s*<span class="meta-badge">\s*<a href="/player/[^"]+"[^>]*>Watch Locally</a>\s*</span>',
        "",
        html,
        flags=re.IGNORECASE,
    )


def _rewrite_back_link(html):
    # Back-to-library link points at localhost. Drop the whole line.
    return re.sub(
        r'<div class="back-link">.*?</div>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _strip_download_badge(html):
    # The Download for sharing badge points back at our /library/<id>/download
    # endpoint — useless (and confusing) in the shared copy.
    return re.sub(
        r'\s*<span class="meta-badge">\s*<a href="/library/[^"]+/download"[^>]*>Download for sharing</a>\s*</span>',
        "",
        html,
        flags=re.IGNORECASE,
    )


def _format_seconds(total):
    total = int(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{m}:{s:02d}]"


def _rewrite_timestamp_placeholder_labels(html):
    # <a href="...&t=NNNs" ...>[timestamp]</a>  ->  <a ...>[MM:SS]</a>
    # Also handles bare literal "timestamp" (no brackets) in case the LLM
    # rendered it either way.
    pattern = re.compile(
        r'(<a\s+href="[^"]*[?&]t=(\d+)s[^"]*"[^>]*>)\s*\[?timestamp\]?\s*(</a>)',
        flags=re.IGNORECASE,
    )
    def _repl(m):
        secs = int(m.group(2))
        return f"{m.group(1)}{_format_seconds(secs)}{m.group(3)}"
    return pattern.sub(_repl, html)


def _neutralize_local_player_timestamp_links(html):
    def _repl(m):
        return m.group("label")
    pattern = re.compile(
        r'<a\s+href="/player/[^"]+"[^>]*>(?P<label>[^<]*)</a>',
        flags=re.IGNORECASE,
    )
    return pattern.sub(_repl, html)


def _add_share_notice(html, notice_html):
    if "</h1>" in html:
        return html.replace("</h1>", "</h1>\n" + notice_html, 1)
    return notice_html + html


_LOCAL_NOTICE = (
    '<p style="background:#fff3cd;border:1px solid #ffeeba;'
    'padding:8px 12px;border-radius:6px;font-size:0.9em;color:#856404;">'
    "Timestamps are shown as plain text in this shared copy because "
    "the source is a local recording (no public URL available)."
    "</p>"
)


def make_shareable_summary(html, video_id):
    """Return HTML suitable for offline sharing."""
    out = _remove_script_block(html)
    out = _strip_yt_player_target(out)
    out = _rewrite_watch_locally_badge(out, video_id)
    out = _strip_download_badge(out)
    out = _rewrite_back_link(out)
    out = _neutralize_transcript_excerpt_links(out, video_id)
    out = _rewrite_timestamp_placeholder_labels(out)
    if _is_local(video_id):
        out = _neutralize_local_player_timestamp_links(out)
        out = _add_share_notice(out, _LOCAL_NOTICE)
    return out
