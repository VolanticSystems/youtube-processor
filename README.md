# YouTube Processor

Turn long-form video content (lectures, patient meetings, panel discussions, interviews) into navigable, source-linked English study material, whether the video is in English, Dutch, or any language the underlying models cover.

## What it does

Point it at a YouTube URL or drop in a local `.mp4`. What comes back is a small library of self-contained artifacts per video:

- A hierarchical **HTML summary** with section headings and bullet points, each anchored to a click-through timestamp in the source video.
- **English subtitles** (WebVTT) that play in any modern browser's video element, produced by an overlap-and-cleanup translation pass tuned to preserve domain-specific terminology.
- A **cleaned, timestamped transcript** alongside the raw one, both English and the original language when they differ.
- A **downloadable, standalone summary** stripped of local-server dependencies, suitable for sharing to someone who does not have the app installed.
- A local video **player page** that jumps to any timestamp in the summary and shows the English subtitle track by default for non-English source material.

Everything lives on disk in a human-readable folder tree (`library/{Category}/{Title} [{id}]/`), so nothing is trapped inside a database.

## Why it exists

The best material on many niche topics is a two-hour panel or an academic lecture recorded on someone's Zoom. YouTube's own auto-translation is workable for casual viewing and unusable for anything where terminology matters. Standalone Whisper is fantastic at what it does but gives you a wall of text, not something you can navigate. Off-the-shelf "summarize this video" tools produce a paragraph and lose the timestamps.

This project pushes each of those pieces to a level where the output is worth keeping: transcription runs locally on a consumer GPU, the summarization step keeps clickable timestamp anchors on every heading and bullet, subtitle translation goes through two overlapping passes and a same-language cleanup so domain terms are not mangled, and the whole thing runs as a Windows service so the library is a bookmark, not a batch job.

## Pipeline

Two entry paths converge on the same output shape.

**YouTube path**

1. `yt-dlp` fetches metadata, chapters, and auto-caption tracks. If YouTube's anti-bot check flags the current IP, the request is retried across a rotation of SOCKS5 proxies until one succeeds.
2. The video's auto-captions are parsed into `{start, duration, text}` segments.
3. If the video has ≥3 real chapters starting near t=0, the chapter-based summarization flow runs. Otherwise the flow that chunks the transcript directly runs. Either way the LLM prompt includes clickable YouTube deep-links (`?t=Ns`) on every heading and bullet.
4. For non-English source, an English subtitle track is generated (see "Translation" below). For English source, subtitles are produced by direct segment-to-VTT conversion; no LLM cost.
5. The mp4 is fetched at up to 1080p on demand, saved next to the summary.

**Local video path**

1. The source `.mp4` is copied into the library. Whisper `large-v3-turbo` transcribes on CUDA (the model is released from VRAM before the next step so a 8GB card can hold both).
2. `pyannote.audio` runs speaker diarization on the same audio; each transcript segment gets a `SPEAKER_N` tag by timestamp overlap.
3. Summarization uses the same chunked flow as the YouTube path, with speaker context in the prompt so headings and bullets can attribute claims when it helps.
4. English subtitles are generated with speaker labels shown when the speaker changes.

Both paths land at `library/{Category}/{Title} [{id}]/` with the same set of files.

## Translation, tuned for material that matters

Long-form video sub-translation is where most tools quietly fall apart. The failure modes are: cues that stack and desync when the model splits sentences differently than the source; hallucinated substitutions in the middle of a passage; and confident mistranslation of domain vocabulary. Three deliberate choices address each:

- **Overlapping chunks.** Every subtitle segment is translated inside two chunks with different neighbors, so no single boundary in the source becomes a boundary in the translation.
- **Same-language cleanup pass.** A second LLM call takes the merged English and the original as input and reconciles them into a coherent whole. This catches lines that stayed in the source language, patched-together grammar, and drifted phrasing.
- **Terminology protection.** The prompt names domain-specific tokens that must survive translation verbatim (protein and molecule names, gene symbols, clinical acronyms). These are extended per-project via `context_hint`.

For English-source video, none of this runs. Subtitles are generated directly from the transcript segments as WebVTT, timestamps preserved exactly, no round-trip cost.

## Architecture

- **Backend**: Flask, single process, run as a Windows service (WinSW) so it starts at boot and survives reboots.
- **Frontend**: server-rendered HTML with progressive-enhancement JavaScript, no build step. A React + Ant Design rebuild is planned for the browsing UI; the current UI is deliberately simple to keep the surface small.
- **Storage**: plain files on disk. The library is a folder tree; each video is a folder with a `meta.json` and a handful of HTML/JSON/VTT/MP4 files. `git`, backup, and file-explorer usage all work as expected.
- **Concurrency**: multiple videos process in parallel via background threads. Local-video work serializes on the GPU (Whisper + pyannote) because one CUDA card, one job at a time.
- **Networking**: SOCKS5 proxy rotation is optional and off by default. When configured, the same rotation applies to metadata fetch, subtitle download, and video download, and rotates on 429/bot-check/sign-in errors.

## Requirements

Local processing is optional. If you only ever process public YouTube URLs, none of the GPU stack is needed.

- Python 3.12+ (venv recommended)
- `ffmpeg` on the `PATH` (used by yt-dlp for muxing and for local audio decoding)
- Firefox with a logged-in YouTube session (yt-dlp reads its cookie store to defeat the "prove you are human" wall on public videos)
- Node.js on the `PATH` (yt-dlp's `n`-challenge solver runs in a Node runtime)
- For local video transcription: CUDA-capable GPU with ≥8 GB VRAM, `faster-whisper`, and `pyannote.audio` with a HuggingFace access token
- An OpenRouter API key (or equivalent) for the summarization/translation LLM calls

## Configuration

Copy `config.yaml` and adjust the model and library location as needed. The following environment variables are honored:

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | LLM access. Required. |
| `HF_TOKEN` | HuggingFace token with read access. Only required for speaker diarization on local videos; you must also accept the `pyannote/speaker-diarization-3.1` terms on huggingface.co. |
| `SOCKS5_PROXIES` | Comma-separated list of full SOCKS5 proxy URLs, e.g. `socks5://user:pass@host1.example.com:1080,socks5://user:pass@host2.example.com:1080`. If unset, requests go direct. |

No credentials are stored in this repository or in the config file. The service reads them from the environment at startup.

## Status

This is a working tool used daily against a real library, not a demo. It is also a **portfolio project by [Volantic Systems](https://github.com/VolanticSystems)** demonstrating how a small, sharp integration of open-source components produces something that off-the-shelf tools cannot. Public issues and pull requests are welcome.

## License

MIT. See [LICENSE](LICENSE).
