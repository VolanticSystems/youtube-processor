"""Flask app for YouTube Processor. Serves the library, video player, and settings."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template_string, request, send_from_directory

from processor import download_video, extract_video_id, fetch_video_title, load_config, process_video

app = Flask(__name__)

# Track processing and download jobs
jobs = {}       # video_id -> {"status": str, "progress": str, "title": str, "meta": dict|None}
downloads = {}  # video_id -> {"status": str, "progress": str, "percent": float}


# ============================================================
# Helpers
# ============================================================

def _get_library_dir():
    """Return the library directory Path from config."""
    config = load_config()
    return Path(config.get("output_dir", "./library"))


def get_categories():
    """Return sorted list of category names (top-level folders in library)."""
    library_dir = _get_library_dir()
    if not library_dir.exists():
        return []
    cats = []
    for child in sorted(library_dir.iterdir()):
        if child.is_dir():
            cats.append(child.name)
    return cats


def get_library_entries():
    """Load all library entries from meta.json files, sorted newest first."""
    library_dir = _get_library_dir()
    if not library_dir.exists():
        return []
    entries = []
    for meta_file in library_dir.glob("*/*/meta.json"):
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        # Derive category from parent directory structure
        meta["_category"] = meta_file.parent.parent.name
        meta["_dir"] = str(meta_file.parent)
        entries.append(meta)
    entries.sort(key=lambda x: x.get("processed_at", ""), reverse=True)
    return entries


def _find_video_dir(video_id):
    """Locate a video's directory by scanning meta.json files for its video_id."""
    library_dir = _get_library_dir()
    for meta_file in library_dir.glob("*/*/meta.json"):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("video_id") == video_id:
                return meta_file.parent
        except Exception:
            continue
    return None


def _update_last_accessed(video_id):
    video_dir = _find_video_dir(video_id)
    if not video_dir:
        return
    meta_path = video_dir / "meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["last_accessed"] = datetime.now().isoformat()
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def _check_dependencies():
    checks = []
    modules = {
        "flask": ("Flask", "Web framework"),
        "yaml": ("PyYAML", "Config file parsing"),
        "requests": ("Requests", "HTTP client for API calls"),
        "yt_dlp": ("yt-dlp", "YouTube video/metadata downloader"),
        "youtube_transcript_api": ("youtube-transcript-api", "Transcript fetching"),
    }
    for mod_name, (display_name, note) in modules.items():
        try:
            mod = __import__(mod_name)
            version = getattr(mod, "__version__", None)
            if version is None:
                version = getattr(mod, "version", None)
                if hasattr(version, "__version__"):
                    version = version.__version__
                elif version is not None:
                    version = str(version)
                else:
                    version = "installed"
            checks.append({"name": display_name, "status": "ok", "version": str(version), "note": note})
        except ImportError:
            checks.append({"name": display_name, "status": "missing", "version": "", "note": note})

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
            first_line = result.stdout.split("\n")[0] if result.stdout else ""
            version_match = re.search(r"ffmpeg version (\S+)", first_line)
            version = version_match.group(1) if version_match else "installed"
            checks.append({"name": "ffmpeg", "status": "ok", "version": version, "note": "Required for 1080p video download (merges video+audio)"})
        except Exception:
            checks.append({"name": "ffmpeg", "status": "ok", "version": "found", "note": "Required for 1080p video download"})
    else:
        checks.append({"name": "ffmpeg", "status": "missing", "version": "", "note": "Without it, video downloads fall back to 720p"})

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if api_key:
        masked = api_key[:4] + "..." + api_key[-4:]
        checks.append({"name": "OPENROUTER_API_KEY", "status": "ok", "version": masked, "note": "API key for AI model access"})
    else:
        checks.append({"name": "OPENROUTER_API_KEY", "status": "missing", "version": "", "note": "Required for AI processing"})

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append({"name": "Python", "status": "ok", "version": py_version, "note": sys.executable})
    return checks


def _format_bytes(size):
    """Format byte count as human-readable string (e.g. '4.2 GB')."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# ============================================================
# HTML Templates
# ============================================================

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube Processor</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 960px; margin: 0 auto; padding: 32px 40px; line-height: 1.6; color: #333; background: #fafafa; }

  /* Header */
  .header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
  h1 { color: #1a1a1a; margin: 0; font-size: 1.7em; }
  .nav-links a { color: #2980b9; text-decoration: none; font-size: 0.9em; font-weight: 500; }
  .nav-links a:hover { text-decoration: underline; }
  .subtitle { color: #888; margin: 0 0 28px 0; font-size: 0.95em; }

  /* Form */
  .add-section { background: #fff; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .add-form { display: flex; gap: 10px; margin-bottom: 12px; }
  .add-form input[type="text"] { flex: 1; padding: 11px 16px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 15px; outline: none; transition: border-color 0.2s; }
  .add-form input[type="text"]:focus { border-color: #2980b9; }
  .add-form button { padding: 11px 28px; background: #2980b9; color: white; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; white-space: nowrap; }
  .add-form button:hover { background: #2471a3; }
  .options { display: flex; gap: 12px; }
  .options .field { flex: 1; }
  .options label { font-size: 0.8em; color: #999; display: block; margin-bottom: 3px; }
  .options input[type="text"] { width: 100%; padding: 9px 14px; border: 1px solid #e0e0e0; border-radius: 6px; font-size: 13px; outline: none; color: #555; transition: border-color 0.2s; }
  .options input[type="text"]:focus { border-color: #2980b9; }
  .options select { width: 100%; padding: 9px 14px; border: 1px solid #e0e0e0; border-radius: 6px; font-size: 13px; outline: none; color: #555; background: white; cursor: pointer; }
  .options select:focus { border-color: #2980b9; }
  .new-category-input { margin-top: 6px; }

  /* Errors and progress */
  .error { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; border-radius: 8px; padding: 12px 20px; margin-bottom: 16px; display: none; }
  .error.active { display: block; }
  .progress-section { margin-bottom: 16px; }
  .progress-card { background: #fff; border: 1px solid #ffc107; border-left: 4px solid #ffc107; border-radius: 8px; padding: 14px 20px; margin-bottom: 10px; }
  .progress-card .job-title { font-weight: 600; font-size: 0.95em; margin-bottom: 6px; color: #333; }
  .progress-bar { height: 5px; background: #e9ecef; border-radius: 3px; overflow: hidden; margin: 6px 0; }
  .progress-fill { height: 100%; background: #2980b9; transition: width 0.3s; }
  .progress-card .job-status { font-size: 0.82em; color: #888; }
  .done-card { border-color: #28a745 !important; border-left-color: #28a745 !important; background: #f0fdf4 !important; }
  .done-card a { color: #155724; font-weight: 600; text-decoration: none; font-size: 0.95em; }
  .done-card a:hover { text-decoration: underline; }
  .error-card { border-color: #dc3545 !important; border-left-color: #dc3545 !important; background: #fef2f2 !important; }

  /* Library */
  .library-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; }
  .library-header h2 { color: #2c3e50; margin: 0; font-size: 1.3em; }
  .library-count { color: #999; font-size: 0.85em; }

  /* Folders */
  .folder-section { margin-bottom: 24px; }
  .folder-bar { display: flex; align-items: center; gap: 8px; padding: 8px 14px; background: #f0f4f8; border-radius: 8px; margin-bottom: 10px; cursor: pointer; user-select: none; }
  .folder-bar:hover { background: #e7eef5; }
  .folder-bar .chevron { display: inline-block; width: 12px; color: #6c7a89; font-size: 0.85em; transition: transform 0.15s; }
  .folder-section.collapsed .folder-bar .chevron { transform: rotate(-90deg); }
  .folder-bar .folder-name { font-weight: 600; color: #2c3e50; font-size: 1.05em; flex: 1; }
  .folder-bar .folder-count { color: #999; font-size: 0.8em; margin-right: 4px; }
  .folder-bar button { background: none; border: none; cursor: pointer; font-size: 0.85em; color: #999; padding: 2px 6px; border-radius: 4px; transition: background 0.15s, color 0.15s; }
  .folder-bar button:hover { background: #e0e0e0; color: #333; }
  .folder-bar .btn-delete:hover { background: #f8d7da; color: #dc3545; }
  .folder-section.collapsed .folder-content { display: none; }
  .folder-bar .folder-count { color: #999; font-size: 0.8em; margin-right: 4px; }
  .library-header .btn-toggle-all { background: none; border: 1px solid #ddd; cursor: pointer; font-size: 0.8em; color: #666; padding: 3px 10px; border-radius: 4px; margin-left: 8px; }
  .library-header .btn-toggle-all:hover { background: #f0f4f8; color: #2c3e50; }

  /* Video cards */
  .video-card { background: #fff; border: 1px solid #e9ecef; border-radius: 8px; padding: 14px 18px; margin-bottom: 8px; transition: box-shadow 0.15s; display: flex; flex-direction: column; gap: 4px; }
  .video-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.07); }
  .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .card-top a.video-title { color: #1a1a1a; text-decoration: none; font-weight: 600; font-size: 1em; line-height: 1.4; flex: 1; }
  .card-top a.video-title:hover { color: #2980b9; }
  .btn-move { font-size: 0.75em; padding: 2px 8px; border: 1px solid #ddd; border-radius: 4px; color: #888; background: #fafafa; cursor: pointer; flex-shrink: 0; }
  .btn-move:hover { border-color: #2980b9; color: #2980b9; }
  .video-meta { color: #aaa; font-size: 0.8em; display: flex; gap: 14px; }
  .video-notes input { border: none; border-bottom: 1px dashed #ddd; font-size: 0.85em; color: #777; font-style: italic; width: 100%; padding: 2px 0; outline: none; font-family: inherit; background: transparent; }
  .video-notes input:focus { border-bottom-color: #2980b9; color: #333; }
  .video-notes input::placeholder { color: #ccc; }

  .empty-state { color: #bbb; text-align: center; padding: 60px 20px; font-size: 1.05em; }

  /* Library path row */
  .library-path-row { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
  .library-label { font-size: 0.85em; color: #888; font-weight: 600; }
  .library-current { background: #f0f0f0; padding: 4px 10px; border-radius: 4px; font-size: 0.85em; color: #555; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn-browse { padding: 4px 12px; border: 1px solid #2980b9; background: white; color: #2980b9; border-radius: 5px; font-size: 0.8em; font-weight: 600; cursor: pointer; }
  .btn-browse:hover { background: #2980b9; color: white; }

  /* Folder picker modal */
  .picker-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.4); z-index: 1000; display: flex; align-items: center; justify-content: center; }
  .picker-dialog { background: white; border-radius: 10px; width: 520px; max-height: 70vh; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
  .picker-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; border-bottom: 1px solid #eee; }
  .picker-title { font-weight: 700; font-size: 1.05em; color: #1a1a1a; }
  .picker-close { background: none; border: none; font-size: 1.4em; cursor: pointer; color: #999; padding: 0 4px; }
  .picker-close:hover { color: #333; }
  .picker-breadcrumb { padding: 10px 20px; font-size: 0.82em; color: #2980b9; display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 1px solid #f5f5f5; }
  .picker-breadcrumb span { cursor: pointer; }
  .picker-breadcrumb span:hover { text-decoration: underline; }
  .picker-breadcrumb .sep { color: #ccc; cursor: default; margin: 0 2px; }
  .picker-breadcrumb .sep:hover { text-decoration: none; }
  .picker-list { flex: 1; overflow-y: auto; padding: 8px 0; min-height: 200px; max-height: 400px; }
  .picker-item { padding: 8px 20px; cursor: pointer; font-size: 0.9em; display: flex; align-items: center; gap: 8px; }
  .picker-item:hover { background: #f0f4f8; }
  .picker-item .folder-icon { color: #f0c040; font-size: 1.1em; }
  .picker-empty { padding: 20px; text-align: center; color: #bbb; font-size: 0.9em; }
  .picker-footer { padding: 12px 20px; border-top: 1px solid #eee; display: flex; align-items: center; gap: 10px; }
  .picker-selected { flex: 1; font-size: 0.82em; color: #555; background: #f8f8f8; padding: 4px 8px; border-radius: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .picker-confirm { padding: 8px 18px; background: #2980b9; color: white; border: none; border-radius: 5px; font-size: 0.85em; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .picker-confirm:hover { background: #2471a3; }
</style>
</head>
<body>

<div class="header">
  <h1>YouTube Processor</h1>
  <div class="nav-links"><a href="/settings">Settings</a></div>
</div>
<p class="subtitle">Paste a YouTube URL to generate a summary with linked transcripts.</p>
<div class="library-path-row">
  <span class="library-label">Library:</span>
  <code class="library-current" id="libraryPath">{{ library_path }}</code>
  <button class="btn-browse" onclick="openFolderPicker()">Change</button>
</div>
<div id="folderPickerOverlay" class="picker-overlay" style="display:none" onclick="if(event.target===this)closePicker()">
  <div class="picker-dialog">
    <div class="picker-header">
      <span class="picker-title">Select Library Folder</span>
      <button class="picker-close" onclick="closePicker()">&times;</button>
    </div>
    <div class="picker-breadcrumb" id="pickerBreadcrumb"></div>
    <div class="picker-list" id="pickerList"></div>
    <div class="picker-footer">
      <code class="picker-selected" id="pickerSelected"></code>
      <button class="picker-confirm" id="pickerConfirm" onclick="confirmPicker()">Select This Folder</button>
    </div>
  </div>
</div>

<div id="moveDialogOverlay" class="picker-overlay" style="display:none" onclick="if(event.target===this)closeMoveDialog()">
  <div class="picker-dialog" style="width:360px">
    <div class="picker-header">
      <span class="picker-title">Move to Category</span>
      <button class="picker-close" onclick="closeMoveDialog()">&times;</button>
    </div>
    <div class="picker-list" id="moveCategoryList"></div>
  </div>
</div>

<div class="add-section">
  <form class="add-form" id="addForm" onsubmit="return submitVideo(event)">
    <input type="text" id="urlInput" placeholder="Paste YouTube URL here..." autocomplete="off">
    <button type="submit">Process</button>
  </form>
  <div class="options">
    <div class="field">
      <label>Category</label>
      <select id="categorySelect" onchange="handleCategoryChange(this)">
        <option value="">Uncategorized</option>
        {% for folder in folders %}
        <option value="{{ folder }}">{{ folder }}</option>
        {% endfor %}
        <option value="__new__">+ New Category</option>
      </select>
      <input type="text" id="newCategoryInput" class="new-category-input" style="display:none"
             placeholder="Type new category name" onkeydown="if(event.key==='Enter'){addNewCategory();}"
             onblur="cancelNewCategory()">
    </div>
    <div class="field">
      <label>Context hint &mdash; accents, jargon, mixed languages</label>
      <input type="text" id="hintInput" placeholder="optional" autocomplete="off">
    </div>
  </div>
</div>

<div class="error" id="errorBox"></div>
<div class="progress-section" id="progressSection" style="display:none;"></div>

<div class="library-header">
  <h2>Library</h2>
  <span class="library-count">{{ entries|length }} video{{ 's' if entries|length != 1 else '' }}</span>
  <button class="btn-toggle-all" onclick="collapseAllCategories()">Collapse all</button>
  <button class="btn-toggle-all" onclick="expandAllCategories()">Expand all</button>
</div>

{% if entries %}
  {% for folder_name, folder_entries in grouped_entries %}
  <div class="folder-section" data-category="{{ folder_name|e }}">
    <div class="folder-bar" onclick="toggleCategory(event, '{{ folder_name|e }}')">
      <span class="chevron">&#9660;</span>
      <span class="folder-name">{{ folder_name }}</span>
      <span class="folder-count">{{ folder_entries|length }}</span>
      {% if folder_name != 'Uncategorized' %}
      <button onclick="event.stopPropagation(); renameCategory('{{ folder_name|e }}')" title="Rename category">&#9998;</button>
      {% endif %}
    </div>
    <div class="folder-content">
    {% for entry in folder_entries %}
    <div class="video-card">
      <div class="card-top">
        <a class="video-title" href="/library/{{ entry.video_id }}/{{ entry.get('files', {}).get('summary', 'summary.html') }}">{{ entry.title }}</a>
        <button class="btn-move" onclick="openMoveDialog('{{ entry.video_id|e }}', '{{ entry._category|e }}')" title="Move to another category">Move</button>
      </div>
      <div class="video-meta">
        <span>{{ entry.duration_display }}</span>
        <span>{{ entry.processed_at[:10] }}</span>
        {% if entry.video_id.startswith('local_') %}
        <span style="color: #888;">local</span>
        {% else %}
        <a href="{{ entry.url }}" target="_blank" style="color: #2980b9; text-decoration: none;">YouTube</a>
        {% endif %}
      </div>
      <div class="video-notes">
        <input type="text" value="{{ entry.get('notes', '') }}" placeholder="Add a note..."
               onchange="saveField('{{ entry.video_id }}', 'notes', this.value)"
               onkeydown="if(event.key==='Enter')this.blur()">
      </div>
    </div>
    {% endfor %}
    </div>
  </div>
  {% endfor %}
{% else %}
  <div class="empty-state">No videos processed yet. Paste a URL above to get started.</div>
{% endif %}

<script>
var activeJobs = {};

function submitVideo(e) {
  e.preventDefault();
  var url = document.getElementById('urlInput').value.trim();
  if (!url) return false;
  var hint = document.getElementById('hintInput').value.trim();
  var sel = document.getElementById('categorySelect');
  var folder = (sel.value === '__new__') ? '' : sel.value;
  document.getElementById('errorBox').className = 'error';

  fetch('/process', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: url, context_hint: hint, folder: folder})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) {
      document.getElementById('errorBox').textContent = data.error;
      document.getElementById('errorBox').className = 'error active';
      return;
    }
    if (data.status === 'done') {
      window.location.href = '/library/' + data.video_id + '/' + (data.summary_file || 'summary.html');
      return;
    }
    addProgressCard(data.video_id, data.title || data.video_id);
    pollStatus(data.video_id);
  })
  .catch(function(err) {
    document.getElementById('errorBox').textContent = 'Request failed: ' + err;
    document.getElementById('errorBox').className = 'error active';
  });

  document.getElementById('urlInput').value = '';
  return false;
}

function addProgressCard(videoId, title) {
  var existing = document.getElementById('job-' + videoId);
  if (existing) {
    existing.className = 'progress-card';
    existing.innerHTML = '<div class="job-title">' + title + '</div>' +
      '<div class="progress-bar"><div class="progress-fill" id="fill-' + videoId + '"></div></div>' +
      '<div class="job-status" id="status-' + videoId + '">Starting...</div>';
    activeJobs[videoId] = true;
    return;
  }
  var section = document.getElementById('progressSection');
  section.style.display = 'block';
  var card = document.createElement('div');
  card.className = 'progress-card';
  card.id = 'job-' + videoId;
  card.innerHTML = '<div class="job-title">' + title + '</div>' +
    '<div class="progress-bar"><div class="progress-fill" id="fill-' + videoId + '"></div></div>' +
    '<div class="job-status" id="status-' + videoId + '">Starting...</div>';
  section.appendChild(card);
  activeJobs[videoId] = true;
}

function pollStatus(videoId) {
  fetch('/status/' + videoId)
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var statusEl = document.getElementById('status-' + videoId);
    if (statusEl) statusEl.textContent = data.progress || 'Working...';
    if (data.status === 'done') {
      delete activeJobs[videoId];
      var card = document.getElementById('job-' + videoId);
      if (card) {
        card.className = 'progress-card done-card';
        card.innerHTML = '<a href="/library/' + videoId + '/' + (data.summary_file || 'summary.html') + '">' +
          (data.title || videoId) + ' &mdash; Done! Click to view &rarr;</a>';
      }
    } else if (data.status === 'error') {
      delete activeJobs[videoId];
      var card = document.getElementById('job-' + videoId);
      if (card) card.className = 'progress-card error-card';
    } else {
      setTimeout(function() { pollStatus(videoId); }, 2000);
    }
  });
}

function saveField(videoId, field, value) {
  fetch('/update/' + videoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({field: field, value: value})
  });
}

function moveVideo(videoId, folder) {
  fetch('/update/' + videoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({field: 'folder', value: folder})
  }).then(function() { location.reload(); });
}

var COLLAPSED_KEY = 'ytproc_collapsed_categories';

function getCollapsedSet() {
  try {
    var raw = localStorage.getItem(COLLAPSED_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch(e) { return new Set(); }
}

function setCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(Array.from(set))); } catch(e) {}
}

function applyCollapseState() {
  var collapsed = getCollapsedSet();
  document.querySelectorAll('.folder-section').forEach(function(sec) {
    var cat = sec.getAttribute('data-category');
    if (collapsed.has(cat)) sec.classList.add('collapsed');
    else sec.classList.remove('collapsed');
  });
}

function toggleCategory(event, name) {
  if (event && event.target && event.target.tagName === 'BUTTON') return;
  var sec = document.querySelector('.folder-section[data-category="' + CSS.escape(name) + '"]');
  if (!sec) return;
  var collapsed = getCollapsedSet();
  if (sec.classList.toggle('collapsed')) collapsed.add(name);
  else collapsed.delete(name);
  setCollapsedSet(collapsed);
}

function collapseAllCategories() {
  var collapsed = new Set();
  document.querySelectorAll('.folder-section').forEach(function(sec) {
    sec.classList.add('collapsed');
    collapsed.add(sec.getAttribute('data-category'));
  });
  setCollapsedSet(collapsed);
}

function expandAllCategories() {
  document.querySelectorAll('.folder-section').forEach(function(sec) {
    sec.classList.remove('collapsed');
  });
  setCollapsedSet(new Set());
}

document.addEventListener('DOMContentLoaded', applyCollapseState);

function renameCategory(oldName) {
  var newName = prompt('Rename category "' + oldName + '" to:', oldName);
  if (!newName || newName.trim() === '' || newName.trim() === oldName) return;
  fetch('/rename-folder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_name: oldName, new_name: newName.trim()})
  }).then(function() { location.reload(); });
}

function deleteCategory(name) {
  if (!confirm('Remove category "' + name + '"?\\nVideos will be moved to Uncategorized.')) return;
  fetch('/delete-folder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({folder_name: name})
  }).then(function() { location.reload(); });
}

var moveVideoId = '';
var moveCurrentCat = '';

function openMoveDialog(videoId, currentCategory) {
  moveVideoId = videoId;
  moveCurrentCat = currentCategory;
  var list = document.getElementById('moveCategoryList');
  list.innerHTML = '';

  var categories = [{% for f in folders %}'{{ f|e }}',{% endfor %}];
  if (categories.indexOf('Uncategorized') === -1) categories.push('Uncategorized');

  for (var i = 0; i < categories.length; i++) {
    var cat = categories[i];
    var item = document.createElement('div');
    item.className = 'picker-item';
    if (cat === currentCategory) {
      item.innerHTML = '<span class="folder-icon">&#128193;</span><strong>' + cat + '</strong> <span style="color:#999;font-size:0.8em">(current)</span>';
    } else {
      item.innerHTML = '<span class="folder-icon">&#128193;</span>' + cat;
      item.dataset.cat = cat;
      item.onclick = function() { doMove(this.dataset.cat); };
    }
    list.appendChild(item);
  }

  document.getElementById('moveDialogOverlay').style.display = 'flex';
}

function closeMoveDialog() {
  document.getElementById('moveDialogOverlay').style.display = 'none';
}

function doMove(newCategory) {
  fetch('/update/' + moveVideoId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({field: 'folder', value: newCategory})
  }).then(function() { location.reload(); });
}

function handleCategoryChange(sel) {
  if (sel.value === '__new__') {
    sel.style.display = 'none';
    var inp = document.getElementById('newCategoryInput');
    inp.style.display = 'block';
    inp.focus();
  }
}

function addNewCategory() {
  var inp = document.getElementById('newCategoryInput');
  var name = inp.value.trim();
  var sel = document.getElementById('categorySelect');
  if (name) {
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    opt.selected = true;
    sel.insertBefore(opt, sel.querySelector('option[value="__new__"]'));
  } else {
    sel.value = '';
  }
  inp.style.display = 'none';
  inp.value = '';
  sel.style.display = '';
}

function cancelNewCategory() {
  var inp = document.getElementById('newCategoryInput');
  var sel = document.getElementById('categorySelect');
  if (inp.value.trim()) {
    addNewCategory();
  } else {
    inp.style.display = 'none';
    sel.value = '';
    sel.style.display = '';
  }
}

/* Folder picker */
var pickerCurrentPath = '';

function openFolderPicker() {
  document.getElementById('folderPickerOverlay').style.display = 'flex';
  var current = document.getElementById('libraryPath').textContent;
  browseTo(current);
}

function closePicker() {
  document.getElementById('folderPickerOverlay').style.display = 'none';
}

function browseTo(path) {
  fetch('/browse-folders?path=' + encodeURIComponent(path))
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { alert(data.error); return; }
    pickerCurrentPath = data.path;
    document.getElementById('pickerSelected').textContent = data.path || 'Select a drive';

    /* breadcrumb */
    var bc = document.getElementById('pickerBreadcrumb');
    bc.innerHTML = '';
    if (data.path) {
      var parts = data.path.replace(/\\\\/g, '/').split('/').filter(Boolean);
      var built = '';
      /* Root / drives link */
      var rootSpan = document.createElement('span');
      rootSpan.textContent = 'Drives';
      rootSpan.onclick = function() { browseTo(''); };
      bc.appendChild(rootSpan);
      for (var i = 0; i < parts.length; i++) {
        var sep = document.createElement('span');
        sep.className = 'sep';
        sep.textContent = ' > ';
        bc.appendChild(sep);
        built += parts[i] + '/';
        var crumb = document.createElement('span');
        crumb.textContent = parts[i];
        crumb.dataset.path = built;
        crumb.onclick = function() { browseTo(this.dataset.path); };
        bc.appendChild(crumb);
      }
    }

    /* directory listing */
    var list = document.getElementById('pickerList');
    list.innerHTML = '';
    if (data.dirs.length === 0) {
      list.innerHTML = '<div class="picker-empty">No subdirectories</div>';
      return;
    }
    for (var j = 0; j < data.dirs.length; j++) {
      var item = document.createElement('div');
      item.className = 'picker-item';
      var fullPath = data.path ? data.path + '\\\\' + data.dirs[j] : data.dirs[j];
      item.dataset.path = fullPath;
      item.innerHTML = '<span class="folder-icon">&#128193;</span>' + data.dirs[j];
      item.onclick = function() { browseTo(this.dataset.path); };
      list.appendChild(item);
    }
  });
}

function confirmPicker() {
  if (!pickerCurrentPath) return;
  fetch('/set-library-path', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: pickerCurrentPath})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { alert(data.error); return; }
    document.getElementById('libraryPath').textContent = data.path;
    closePicker();
  });
}
</script>
</body>
</html>"""


PLAYER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} - Player</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #111; color: #fff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; height: 100vh; display: flex; flex-direction: column; }
  .title-bar { background: #1a1a1a; padding: 8px 16px; font-size: 0.9em; color: #aaa; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0; }
  .player-wrap { flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  video { max-width: 100%; max-height: 100%; }
  .download-box { text-align: center; padding: 40px; }
  .download-box h2 { margin-bottom: 16px; font-weight: 400; }
  .dl-progress { width: 400px; max-width: 80vw; height: 8px; background: #333; border-radius: 4px; overflow: hidden; margin: 20px auto; }
  .dl-fill { height: 100%; background: #2980b9; transition: width 0.3s; width: 0%; }
  .dl-text { color: #888; font-size: 0.9em; }
</style>
</head>
<body>
<div class="title-bar">{{ title }}</div>
<div class="player-wrap">
  {% if has_video %}
  <video id="vid" controls autoplay>
    <source src="/library/{{ video_id }}/video.mp4" type="video/mp4">
    {% if has_subs %}<track label="English" kind="subtitles" srclang="en" src="/library/{{ video_id }}/subs_en.vtt" {{ 'default' if subs_default else '' }}>{% endif %}
  </video>
  {% else %}
  <div class="download-box" id="dlBox">
    <h2>Video not cached</h2>
    <div class="dl-progress"><div class="dl-fill" id="dlFill"></div></div>
    <p class="dl-text" id="dlText">Starting download...</p>
  </div>
  {% endif %}
</div>
<script>
function seekTo(seconds) {
  var v = document.getElementById('vid');
  if (!v) return;
  v.currentTime = seconds;
  v.play();
}

function handleHash() {
  var m = window.location.hash.match(/t=(\\d+)/);
  if (m) seekTo(parseInt(m[1]));
}

window.addEventListener('hashchange', handleHash);

{% if has_video %}
var vid = document.getElementById('vid');
vid.addEventListener('loadedmetadata', handleHash);
if (vid.readyState >= 1) handleHash();
{% else %}
fetch('/download/{{ video_id }}', {method: 'POST'})
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.status === 'exists') { location.reload(); return; }
    pollDl();
  });

function pollDl() {
  fetch('/download-status/{{ video_id }}')
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var txt = document.getElementById('dlText');
    var fill = document.getElementById('dlFill');
    if (txt) txt.textContent = d.progress || 'Downloading...';
    if (fill) fill.style.width = (d.percent || 0) + '%';
    if (d.status === 'done') {
      location.reload();
    } else if (d.status === 'error') {
      if (txt) txt.textContent = 'Error: ' + d.progress;
    } else {
      setTimeout(pollDl, 1000);
    }
  });
}
{% endif %}
</script>
</body>
</html>"""


SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Settings</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 960px; margin: 0 auto; padding: 32px 40px; line-height: 1.6; color: #333; background: #fafafa; }
  h1 { color: #1a1a1a; margin: 0 0 24px 0; font-size: 1.7em; }
  .back-link { display: inline-block; margin-bottom: 16px; }
  .back-link a { color: #2980b9; text-decoration: none; font-weight: 600; font-size: 0.9em; }
  .back-link a:hover { text-decoration: underline; }

  .section { background: #fff; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .section h2 { color: #2c3e50; margin: 0 0 16px 0; font-size: 1.15em; padding-bottom: 8px; border-bottom: 1px solid #eee; }

  /* Storage */
  .storage-summary { display: flex; gap: 24px; align-items: baseline; margin-bottom: 16px; }
  .storage-summary .big-num { font-size: 1.8em; font-weight: 700; color: #1a1a1a; }
  .storage-summary .sub { color: #888; font-size: 0.9em; }
  .trim-controls { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  .trim-controls label { font-size: 0.9em; color: #666; }
  .trim-controls input { width: 70px; padding: 6px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; text-align: center; }
  .trim-controls button { padding: 6px 16px; border: none; border-radius: 5px; font-size: 0.85em; font-weight: 600; cursor: pointer; }
  .btn-preview { background: #2980b9; color: white; }
  .btn-preview:hover { background: #2471a3; }
  .btn-confirm { background: #dc3545; color: white; display: none; }
  .btn-confirm:hover { background: #c82333; }
  .btn-reset { background: #999; color: white; display: none; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #eee; font-size: 0.78em; color: #999; text-transform: uppercase; letter-spacing: 0.04em; }
  td { padding: 8px 10px; border-bottom: 1px solid #f5f5f5; font-size: 0.9em; }
  tr.mark-delete { background: #fff8e1; }
  tr.mark-delete td:first-child::before { content: "\\2717 "; color: #dc3545; font-weight: bold; }

  /* Dependencies */
  .dep-ok { color: #28a745; font-weight: 600; }
  .dep-missing { color: #dc3545; font-weight: 600; }
  .dep-version { color: #888; font-family: monospace; font-size: 0.82em; }
  .dep-note { color: #aaa; font-size: 0.82em; }

  .empty-state { color: #bbb; text-align: center; padding: 24px; }

  /* Library path row */
  .library-path-row { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .library-label { font-size: 0.85em; color: #888; font-weight: 600; }
  .library-current { background: #f0f0f0; padding: 4px 10px; border-radius: 4px; font-size: 0.85em; color: #555; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn-browse { padding: 4px 12px; border: 1px solid #2980b9; background: white; color: #2980b9; border-radius: 5px; font-size: 0.8em; font-weight: 600; cursor: pointer; }
  .btn-browse:hover { background: #2980b9; color: white; }

  /* Folder picker modal */
  .picker-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.4); z-index: 1000; display: flex; align-items: center; justify-content: center; }
  .picker-dialog { background: white; border-radius: 10px; width: 520px; max-height: 70vh; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
  .picker-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; border-bottom: 1px solid #eee; }
  .picker-title { font-weight: 700; font-size: 1.05em; color: #1a1a1a; }
  .picker-close { background: none; border: none; font-size: 1.4em; cursor: pointer; color: #999; padding: 0 4px; }
  .picker-close:hover { color: #333; }
  .picker-breadcrumb { padding: 10px 20px; font-size: 0.82em; color: #2980b9; display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 1px solid #f5f5f5; }
  .picker-breadcrumb span { cursor: pointer; }
  .picker-breadcrumb span:hover { text-decoration: underline; }
  .picker-breadcrumb .sep { color: #ccc; cursor: default; margin: 0 2px; }
  .picker-breadcrumb .sep:hover { text-decoration: none; }
  .picker-list { flex: 1; overflow-y: auto; padding: 8px 0; min-height: 200px; max-height: 400px; }
  .picker-item { padding: 8px 20px; cursor: pointer; font-size: 0.9em; display: flex; align-items: center; gap: 8px; }
  .picker-item:hover { background: #f0f4f8; }
  .picker-item .folder-icon { color: #f0c040; font-size: 1.1em; }
  .picker-empty { padding: 20px; text-align: center; color: #bbb; font-size: 0.9em; }
  .picker-footer { padding: 12px 20px; border-top: 1px solid #eee; display: flex; align-items: center; gap: 10px; }
  .picker-selected { flex: 1; font-size: 0.82em; color: #555; background: #f8f8f8; padding: 4px 8px; border-radius: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .picker-confirm { padding: 8px 18px; background: #2980b9; color: white; border: none; border-radius: 5px; font-size: 0.85em; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .picker-confirm:hover { background: #2471a3; }
</style>
</head>
<body>
<div class="back-link"><a href="/">&larr; Back to Library</a></div>
<h1>Settings</h1>

<div class="section">
  <h2>Video Storage</h2>
  <div class="storage-summary">
    <span class="big-num">{{ total_size_display }}</span>
    <span class="sub">{{ video_count }} cached video{{ 's' if video_count != 1 else '' }}</span>
  </div>
  <div class="library-path-row">
    <span class="library-label">Library:</span>
    <code class="library-current" id="libraryPath">{{ library_path }}</code>
    <button class="btn-browse" onclick="openFolderPicker()">Change</button>
  </div>
  <div id="folderPickerOverlay" class="picker-overlay" style="display:none" onclick="if(event.target===this)closePicker()">
    <div class="picker-dialog">
      <div class="picker-header">
        <span class="picker-title">Select Library Folder</span>
        <button class="picker-close" onclick="closePicker()">&times;</button>
      </div>
      <div class="picker-breadcrumb" id="pickerBreadcrumb"></div>
      <div class="picker-list" id="pickerList"></div>
      <div class="picker-footer">
        <code class="picker-selected" id="pickerSelected"></code>
        <button class="picker-confirm" id="pickerConfirm" onclick="confirmPicker()">Select This Folder</button>
      </div>
    </div>
  </div>

  {% if videos %}
  <div class="trim-controls">
    <label>Target:</label>
    <input type="number" id="targetGB" value="{{ total_size_gb }}" step="0.1" min="0">
    <label>GB</label>
    <button class="btn-preview" onclick="previewTrim()">Preview Trim</button>
    <button class="btn-confirm" id="confirmBtn" onclick="confirmTrim()">Delete Selected</button>
    <button class="btn-reset" id="resetBtn" onclick="resetTrim()">Reset</button>
  </div>
  <table>
    <thead><tr><th>Video</th><th>Size</th><th>Last Accessed</th><th>Duration</th></tr></thead>
    <tbody id="videoTable">
      {% for v in videos %}
      <tr id="row-{{ v.video_id }}" data-size="{{ v.size_bytes }}" data-id="{{ v.video_id }}">
        <td>{{ v.title }}</td>
        <td>{{ v.size_display }}</td>
        <td>{{ v.last_accessed_display }}</td>
        <td>{{ v.duration_display }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty-state">No cached videos yet.</div>
  {% endif %}
</div>

<div class="section">
  <h2>System Dependencies</h2>
  <table>
    <thead><tr><th>Component</th><th>Status</th><th>Version</th><th>Notes</th></tr></thead>
    <tbody>
      {% for dep in deps %}
      <tr>
        <td>{{ dep.name }}</td>
        <td class="{{ 'dep-ok' if dep.status == 'ok' else 'dep-missing' }}">{{ dep.status }}</td>
        <td class="dep-version">{{ dep.version }}</td>
        <td class="dep-note">{{ dep.note }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<script>
var toDelete = [];

function previewTrim() {
  resetTrim();
  var targetBytes = parseFloat(document.getElementById('targetGB').value) * 1073741824;
  var rows = document.querySelectorAll('#videoTable tr');
  var total = 0;
  rows.forEach(function(r) { total += parseInt(r.dataset.size || 0); });
  if (total <= targetBytes) { alert('Already under target.'); return; }

  var freed = 0, needed = total - targetBytes;
  for (var i = 0; i < rows.length && freed < needed; i++) {
    rows[i].classList.add('mark-delete');
    freed += parseInt(rows[i].dataset.size || 0);
    toDelete.push(rows[i].dataset.id);
  }
  if (toDelete.length > 0) {
    document.getElementById('confirmBtn').style.display = 'inline-block';
    document.getElementById('resetBtn').style.display = 'inline-block';
  }
}

function resetTrim() {
  toDelete = [];
  document.querySelectorAll('.mark-delete').forEach(function(r) { r.classList.remove('mark-delete'); });
  document.getElementById('confirmBtn').style.display = 'none';
  document.getElementById('resetBtn').style.display = 'none';
}

function confirmTrim() {
  if (!confirm('Delete ' + toDelete.length + ' video file(s)? Summaries and transcripts will be kept.')) return;
  fetch('/settings/trim', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({video_ids: toDelete})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) { if (d.ok) location.reload(); });
}

/* Folder picker */
var pickerCurrentPath = '';

function openFolderPicker() {
  document.getElementById('folderPickerOverlay').style.display = 'flex';
  var current = document.getElementById('libraryPath').textContent;
  browseTo(current);
}

function closePicker() {
  document.getElementById('folderPickerOverlay').style.display = 'none';
}

function browseTo(path) {
  fetch('/browse-folders?path=' + encodeURIComponent(path))
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { alert(data.error); return; }
    pickerCurrentPath = data.path;
    document.getElementById('pickerSelected').textContent = data.path || 'Select a drive';

    var bc = document.getElementById('pickerBreadcrumb');
    bc.innerHTML = '';
    if (data.path) {
      var parts = data.path.replace(/\\\\/g, '/').split('/').filter(Boolean);
      var built = '';
      var rootSpan = document.createElement('span');
      rootSpan.textContent = 'Drives';
      rootSpan.onclick = function() { browseTo(''); };
      bc.appendChild(rootSpan);
      for (var i = 0; i < parts.length; i++) {
        var sep = document.createElement('span');
        sep.className = 'sep';
        sep.textContent = ' > ';
        bc.appendChild(sep);
        built += parts[i] + '/';
        var crumb = document.createElement('span');
        crumb.textContent = parts[i];
        crumb.dataset.path = built;
        crumb.onclick = function() { browseTo(this.dataset.path); };
        bc.appendChild(crumb);
      }
    }

    var list = document.getElementById('pickerList');
    list.innerHTML = '';
    if (data.dirs.length === 0) {
      list.innerHTML = '<div class="picker-empty">No subdirectories</div>';
      return;
    }
    for (var j = 0; j < data.dirs.length; j++) {
      var item = document.createElement('div');
      item.className = 'picker-item';
      var fullPath = data.path ? data.path + '\\\\' + data.dirs[j] : data.dirs[j];
      item.dataset.path = fullPath;
      item.innerHTML = '<span class="folder-icon">&#128193;</span>' + data.dirs[j];
      item.onclick = function() { browseTo(this.dataset.path); };
      list.appendChild(item);
    }
  });
}

function confirmPicker() {
  if (!pickerCurrentPath) return;
  fetch('/set-library-path', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: pickerCurrentPath})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { alert(data.error); return; }
    document.getElementById('libraryPath').textContent = data.path;
    closePicker();
    location.reload();
  });
}
</script>
</body>
</html>"""


# ============================================================
# Routes
# ============================================================

@app.route("/")
def index():
    entries = get_library_entries()
    categories = get_categories()

    grouped = {}
    for entry in entries:
        cat = entry.get("_category", "Uncategorized")
        grouped.setdefault(cat, []).append(entry)

    grouped_entries = []
    for cname in sorted(k for k in grouped if k != "Uncategorized"):
        grouped_entries.append((cname, grouped[cname]))
    if "Uncategorized" in grouped:
        grouped_entries.append(("Uncategorized", grouped["Uncategorized"]))

    library_path = str(_get_library_dir().resolve())
    return render_template_string(INDEX_HTML, entries=entries, folders=categories, grouped_entries=grouped_entries, library_path=library_path)


@app.route("/process", methods=["POST"])
def start_processing():
    data = request.get_json()
    url = data.get("url", "").strip()
    context_hint = data.get("context_hint", "").strip()
    category = data.get("folder", "").strip()

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not extract a valid YouTube video ID from that URL."})

    if video_id in jobs and jobs[video_id]["status"] == "processing":
        return jsonify({"video_id": video_id, "status": "processing", "title": jobs[video_id].get("title", video_id)})

    # Check if already processed
    existing = _find_video_dir(video_id)
    if existing and (existing / "meta.json").exists():
        return jsonify({"video_id": video_id, "status": "done"})

    title = fetch_video_title(video_id)
    jobs[video_id] = {"status": "processing", "progress": "Starting...", "title": title, "meta": None}

    def run():
        try:
            def on_progress(msg):
                jobs[video_id]["progress"] = msg

            meta = process_video(video_id, progress_callback=on_progress, context_hint=context_hint, category=category)
            jobs[video_id]["status"] = "done"
            jobs[video_id]["meta"] = meta
        except Exception as e:
            jobs[video_id]["status"] = "error"
            jobs[video_id]["progress"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"video_id": video_id, "status": "processing", "title": title})


@app.route("/status/<video_id>")
def get_status(video_id):
    def _summary_file_for(vdir):
        meta_path = vdir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta.get("files", {}).get("summary")
        except Exception:
            return None

    if video_id in jobs:
        job = jobs[video_id]
        resp = {"status": job["status"], "progress": job["progress"], "title": job.get("title", video_id)}
        if job["status"] == "done":
            existing = _find_video_dir(video_id)
            if existing:
                sf = _summary_file_for(existing)
                if sf:
                    resp["summary_file"] = sf
        return jsonify(resp)

    existing = _find_video_dir(video_id)
    if existing and (existing / "meta.json").exists():
        resp = {"status": "done", "progress": "Done!"}
        sf = _summary_file_for(existing)
        if sf:
            resp["summary_file"] = sf
        return jsonify(resp)
    return jsonify({"status": "unknown", "progress": "No job found"})


@app.route("/update/<video_id>", methods=["POST"])
def update_meta(video_id):
    video_dir = _find_video_dir(video_id)
    if not video_dir:
        return jsonify({"error": "Video not found"}), 404

    data = request.get_json()
    field = data.get("field", "")
    value = data.get("value", "")

    if field not in {"notes", "folder"}:
        return jsonify({"error": f"Cannot update field: {field}"}), 400

    if field == "folder":
        # "folder" means category; move the directory to a new category
        library_dir = _get_library_dir()
        new_cat = value.strip() if value.strip() else "Uncategorized"
        new_cat_dir = library_dir / new_cat
        new_cat_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_cat_dir / video_dir.name
        if new_path.exists():
            # Avoid collision
            counter = 1
            while new_path.exists():
                new_path = new_cat_dir / f"{video_dir.name} ({counter})"
                counter += 1
        shutil.move(str(video_dir), str(new_path))
        # Clean up empty old category folder
        old_cat_dir = video_dir.parent
        if old_cat_dir.exists() and not any(old_cat_dir.iterdir()):
            old_cat_dir.rmdir()
        return jsonify({"ok": True})

    meta_path = video_dir / "meta.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    meta[field] = value
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return jsonify({"ok": True})


@app.route("/rename-folder", methods=["POST"])
def rename_folder():
    data = request.get_json()
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "Both old and new names required"}), 400

    library_dir = _get_library_dir()
    old_dir = library_dir / old_name
    new_dir = library_dir / new_name
    if not old_dir.exists():
        return jsonify({"error": "Category not found"}), 404
    if new_dir.exists():
        return jsonify({"error": "A category with that name already exists"}), 400
    old_dir.rename(new_dir)
    return jsonify({"ok": True})


@app.route("/delete-folder", methods=["POST"])
def delete_folder():
    data = request.get_json()
    folder_name = data.get("folder_name", "").strip()
    if not folder_name:
        return jsonify({"error": "Category name required"}), 400

    library_dir = _get_library_dir()
    cat_dir = library_dir / folder_name
    if not cat_dir.exists():
        return jsonify({"error": "Category not found"}), 404

    # Move all videos to Uncategorized
    uncat_dir = library_dir / "Uncategorized"
    uncat_dir.mkdir(parents=True, exist_ok=True)
    for child in cat_dir.iterdir():
        if child.is_dir():
            dest = uncat_dir / child.name
            if dest.exists():
                counter = 1
                while dest.exists():
                    dest = uncat_dir / f"{child.name} ({counter})"
                    counter += 1
            shutil.move(str(child), str(dest))
    # Remove the now-empty category folder
    if cat_dir.exists() and not any(cat_dir.iterdir()):
        cat_dir.rmdir()
    return jsonify({"ok": True})


@app.route("/player/<video_id>")
def player(video_id):
    video_dir = _find_video_dir(video_id)
    has_video = video_dir is not None and (video_dir / "video.mp4").exists()

    title = video_id
    has_subs = False
    subs_default = False
    source_lang = "en"
    if video_dir:
        meta_path = video_dir / "meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                title = meta.get("title", video_id)
                source_lang = meta.get("source_lang", "en")
            except Exception:
                pass
        has_subs = (video_dir / "subs_en.vtt").exists()
        subs_default = has_subs and source_lang != "en"

    _update_last_accessed(video_id)
    return render_template_string(PLAYER_HTML, video_id=video_id, has_video=has_video, has_subs=has_subs, subs_default=subs_default, title=title)


@app.route("/download/<video_id>", methods=["POST"])
def start_download(video_id):
    video_dir = _find_video_dir(video_id)
    if not video_dir:
        return jsonify({"status": "error", "progress": "Video not processed yet"}), 404

    if (video_dir / "video.mp4").exists():
        return jsonify({"status": "exists"})
    if video_id in downloads and downloads[video_id]["status"] == "downloading":
        return jsonify({"status": "downloading"})

    downloads[video_id] = {"status": "downloading", "progress": "Starting...", "percent": 0}

    def run():
        try:
            def on_progress(msg):
                downloads[video_id]["progress"] = msg
                pct_match = re.search(r"([\d.]+)%", msg)
                if pct_match:
                    downloads[video_id]["percent"] = float(pct_match.group(1))

            download_video(video_id, str(video_dir), progress_callback=on_progress)
            downloads[video_id] = {"status": "done", "progress": "Done!", "percent": 100}
        except Exception as e:
            downloads[video_id] = {"status": "error", "progress": str(e), "percent": 0}

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "downloading"})


@app.route("/download-status/<video_id>")
def get_download_status(video_id):
    if video_id in downloads:
        return jsonify(downloads[video_id])
    video_dir = _find_video_dir(video_id)
    if video_dir and (video_dir / "video.mp4").exists():
        return jsonify({"status": "done", "progress": "Done!", "percent": 100})
    return jsonify({"status": "unknown", "progress": "No download in progress", "percent": 0})


@app.route("/settings")
def settings():
    config = load_config()
    library_dir = Path(config.get("output_dir", "./library"))

    videos = []
    total_bytes = 0

    if library_dir.exists():
        for meta_file in library_dir.glob("*/*/meta.json"):
            video_path = meta_file.parent / "video.mp4"
            if not video_path.exists():
                continue
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            size = video_path.stat().st_size
            total_bytes += size
            last_accessed = meta.get("last_accessed", meta.get("processed_at", "unknown"))
            videos.append({
                "video_id": meta["video_id"],
                "title": meta.get("title", meta["video_id"]),
                "size_bytes": size,
                "size_display": _format_bytes(size),
                "last_accessed": last_accessed,
                "last_accessed_display": last_accessed[:10] if len(last_accessed) >= 10 else last_accessed,
                "duration_display": meta.get("duration_display", ""),
            })

    videos.sort(key=lambda x: x["last_accessed"])
    total_gb = total_bytes / (1024 ** 3)

    return render_template_string(
        SETTINGS_HTML,
        videos=videos,
        video_count=len(videos),
        total_size_display=_format_bytes(total_bytes),
        total_size_gb=f"{total_gb:.1f}",
        deps=_check_dependencies(),
        library_path=str(library_dir.resolve()),
    )


@app.route("/settings/trim", methods=["POST"])
def settings_trim():
    data = request.get_json()
    video_ids = data.get("video_ids", [])
    config = load_config()
    library_dir = Path(config.get("output_dir", "./library"))

    deleted = 0
    for vid in video_ids:
        if not re.match(r"^[a-zA-Z0-9_-]+$", vid):
            continue
        video_dir = _find_video_dir(vid)
        if video_dir:
            video_path = video_dir / "video.mp4"
            if video_path.exists():
                video_path.unlink()
                deleted += 1
    return jsonify({"ok": True, "deleted": deleted})


# Keep old /storage URL working as redirect
@app.route("/storage")
def storage_redirect():
    from flask import redirect
    return redirect("/settings")


@app.route("/library")
@app.route("/library/")
def library_redirect():
    from flask import redirect
    return redirect("/", code=301)


@app.route("/add-category", methods=["POST"])
def add_category():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Category name required"}), 400
    library_dir = _get_library_dir()
    cat_dir = library_dir / name
    if cat_dir.exists():
        return jsonify({"error": "Category already exists"}), 400
    cat_dir.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "name": name})


@app.route("/browse-folders")
def browse_folders():
    """Return subdirectories of a given path for the folder picker."""
    requested = request.args.get("path", "").strip()
    if not requested:
        # Return drive roots on Windows
        import string
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if Path(drive).exists():
                drives.append(drive)
        return jsonify({"path": "", "dirs": drives, "parent": None})

    p = Path(requested).resolve()
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Not a valid directory"}), 400

    dirs = []
    try:
        for child in sorted(p.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                dirs.append(child.name)
    except PermissionError:
        pass

    parent = str(p.parent) if p.parent != p else None
    return jsonify({"path": str(p), "dirs": dirs, "parent": parent})


@app.route("/set-library-path", methods=["POST"])
def set_library_path():
    """Update the output_dir in config.yaml."""
    data = request.get_json()
    new_path = data.get("path", "").strip()
    if not new_path:
        return jsonify({"error": "No path provided"}), 400

    p = Path(new_path)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Directory does not exist"}), 400

    config_path = Path(__file__).parent / "config.yaml"
    config = load_config()
    config["output_dir"] = str(p)
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False)

    return jsonify({"ok": True, "path": str(p.resolve())})


@app.route("/library/<video_id>/<path:filename>")
def serve_library_file(video_id, filename):
    video_dir = _find_video_dir(video_id)
    if not video_dir:
        return "Video not found", 404
    if filename.startswith("summary - ") or filename == "summary.html":
        _update_last_accessed(video_id)
    return send_from_directory(str(video_dir), filename)


@app.route("/library/<video_id>/download")
def download_summary(video_id):
    from flask import Response
    from share_export import make_shareable_summary
    video_dir = _find_video_dir(video_id)
    if not video_dir:
        return "Video not found", 404
    meta_path = video_dir / "meta.json"
    if not meta_path.exists():
        return "Meta not found", 404
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    summary_name = meta.get("files", {}).get("summary")
    if not summary_name:
        return "No summary file recorded", 404
    summary_path = video_dir / summary_name
    if not summary_path.exists():
        return "Summary file missing on disk", 404
    with open(summary_path, "r", encoding="utf-8") as f:
        html = f.read()
    out = make_shareable_summary(html, video_id)
    safe_title = meta.get("safe_title") or video_id
    filename = f"{safe_title}.html"
    return Response(
        out,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    config = load_config()
    port = config.get("port", 5000)
    print(f"YouTube Processor running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
