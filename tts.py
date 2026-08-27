#!/usr/bin/env python3
"""
epub_to_audiobook.py
Convert an EPUB file to chapter-wise audiobook WAV files using Kokoro TTS.

Usage:
    python epub_to_audiobook.py book.epub
    python epub_to_audiobook.py book.epub --voice af_heart --lang a --output ./audiobook
    python epub_to_audiobook.py book.epub --list-voices

Requirements:
    pip install kokoro>=0.9.4 soundfile ebooklib beautifulsoup4 numpy
    apt-get install espeak-ng   # Linux only, needed by kokoro
"""

import argparse
import os
import re
import sys
import threading
import unicodedata
from pathlib import Path
import zipfile

import ebooklib
import numpy as np
import soundfile as sf
from bs4 import BeautifulSoup
from ebooklib import epub
from kokoro import KPipeline

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 24000
SILENCE_BETWEEN_SEGMENTS = 0.4  # seconds of silence to stitch between chunks
SILENCE_BETWEEN_PARAGRAPHS = 0.4  # seconds of silence between paragraphs

AVAILABLE_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "am_adam", "am_michael",
    "bf_emma", "bf_isabella",
    "bm_george", "bm_lewis",
]


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def html_to_text(html_content: str) -> str:
    """Extract clean plain text from an HTML/XHTML chapter."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove non-content tags entirely
    for tag in soup(["script", "style", "meta", "link", "head",
                     "nav", "aside", "figure", "figcaption", "sup", "sub"]):
        tag.decompose()

    # Only collect LEAF block elements — ones that contain no further block
    # descendants. This prevents text from being emitted multiple times when
    # tags are nested (e.g. <div><p>text</p></div> would otherwise produce
    # "text" twice: once for the div and once for the p).
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "blockquote", "td", "th", "pre",
                  "div", "section", "article"}

    blocks = []
    for tag in soup.find_all(BLOCK_TAGS):
        # Skip container tags that have block-level children —
        # the children will be visited and will emit the same text.
        if tag.find(BLOCK_TAGS):
            continue
        text = tag.get_text(separator=" ", strip=True)
        if text:
            blocks.append(text)

    # Fallback: no block structure found (e.g. plain-text EPUB items)
    if not blocks:
        body = soup.find("body") or soup
        raw = body.get_text(separator="\n")
        blocks = [line.strip() for line in raw.splitlines() if line.strip()]

    return "\n\n".join(blocks)


def clean_text(text: str) -> str:
    """Normalise Unicode, collapse whitespace, remove TTS-hostile characters."""
    text = unicodedata.normalize("NFKC", text)
    # Replace various dashes with a regular hyphen-space for better TTS
    text = re.sub(r"[—–]", " - ", text)
    # Remove soft hyphens
    text = text.replace("\xad", "")
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)
    # Collapse 3+ newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------
def get_chapters(epub_path: str) -> list[dict]:
    """
    Return a list of chapters as dicts with keys: title, text.
    Chapters are ordered by spine order (reading order).
    """
    import zipfile

    # Patch ZipFile.read to skip missing entries (handles broken EPUBs)
    original_read = zipfile.ZipFile.read
    def safe_read(self, name, *args, **kwargs):
        try:
            return original_read(self, name, *args, **kwargs)
        except KeyError:
            print(f"  ⚠  Missing archive entry skipped: {name}")
            return b""
    zipfile.ZipFile.read = safe_read

    try:
        book = epub.read_epub(epub_path)
    finally:
        zipfile.ZipFile.read = original_read

    # Build a map of href → document
    docs_by_href = {}
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        docs_by_href[item.get_name()] = item

    # Follow spine order
    chapters = []
    for spine_id, _ in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is None:
            continue
        # Skip non-document spine entries (ncx / nav / metadata items)
        if not hasattr(item, "get_body_content"):
            continue

        raw_html = item.get_body_content()
        if isinstance(raw_html, bytes):
            raw_html = raw_html.decode("utf-8", errors="replace")

        text = html_to_text(raw_html)
        text = clean_text(text)

        if len(text) < 50:
            continue

        soup = BeautifulSoup(raw_html, "html.parser")
        heading = soup.find(["h1", "h2", "h3"])
        title = heading.get_text(strip=True) if heading else item.get_name()
        if not title:
            title = Path(item.get_name()).stem

        word_count = len(re.findall(r"\b\w+\b", text))
        chapters.append({"title": title, "text": text, "word_count": word_count})

    return chapters

# ---------------------------------------------------------------------------
# TTS synthesis
# ---------------------------------------------------------------------------

def text_to_silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


class StopRequested(Exception):
    """Raised internally to propagate a Ctrl+C cleanly up the call stack."""
    pass


def synthesise_chapter(
    pipeline: KPipeline,
    text: str,
    voice: str,
    chapter_dir: Path,
    chapter_index: int,
    verbose: bool = True,
    progress_cb=None,
    stop_event: threading.Event | None = None,
) -> Path:
    """
    Synthesise one chapter.
    - Splits text into paragraphs.
    - Generates TTS for each paragraph (Kokoro auto-chunks long text).
    - Merges all segments into a single chapter WAV.
    Returns path to the merged chapter WAV.
    Raises StopRequested if Ctrl+C is pressed (or stop_event is set).

    Optional extras (used by the GUI):
    - progress_cb: callable(dict) invoked with
        {"type": "para", "para": n, "total_paras": t, "preview": str}
        after each paragraph completes, and with
        {"type": "chapter_done", "path": str, "duration_min": float}
        when the chapter WAV is written.
    - stop_event: threading.Event; if set, the chapter aborts cleanly
        (partial audio is saved, StopRequested is raised).
    """
    chapter_dir.mkdir(parents=True, exist_ok=True)

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    all_audio: list[np.ndarray] = []
    seg_index = 0

    try:
        for para_num, paragraph in enumerate(paragraphs):
            if stop_event is not None and stop_event.is_set():
                raise StopRequested
            if verbose:
                preview = paragraph[:60].replace("\n", " ")
                print(f"    Para {para_num + 1}/{len(paragraphs)}: {preview}…")

            para_audio: list[np.ndarray] = []

            generator = pipeline(paragraph, voice=voice)
            try:
                for _, _, audio in generator:
                    if audio is not None and len(audio) > 0:
                        para_audio.append(audio)
                        # Optionally save individual segment for debugging
                        seg_path = chapter_dir / f"seg_{seg_index:04d}.wav"
                        sf.write(str(seg_path), audio, SAMPLE_RATE)
                        seg_index += 1
            except KeyboardInterrupt:
                raise StopRequested

            if para_audio:
                # Stitch segments within a paragraph (tiny silence)
                para_combined = np.concatenate(
                    [chunk for pair in zip(
                        para_audio,
                        [text_to_silence(SILENCE_BETWEEN_SEGMENTS)] * len(para_audio)
                    ) for chunk in pair]
                )
                all_audio.append(para_combined)
                # Silence between paragraphs
                all_audio.append(text_to_silence(SILENCE_BETWEEN_PARAGRAPHS))

            if progress_cb:
                progress_cb({
                    "type": "para",
                    "para": para_num + 1,
                    "total_paras": len(paragraphs),
                    "preview": paragraph[:60].replace("\n", " "),
                })

    except (KeyboardInterrupt, StopRequested):
        # Save whatever audio was completed before the interrupt
        if all_audio:
            print(f"\n  ⚠  Interrupted — saving partial chapter {chapter_index + 1}…")
            merged = np.concatenate(all_audio).astype(np.float32)
            output_path = chapter_dir.parent / f"chapter_{chapter_index + 1:03d}_partial.wav"
            sf.write(str(output_path), merged, SAMPLE_RATE)
            duration_min = len(merged) / SAMPLE_RATE / 60
            print(f"  ✓  Partial saved → {output_path.name}  ({duration_min:.1f} min)")
            if progress_cb:
                progress_cb({"type": "chapter_partial", "path": str(output_path), "duration_min": duration_min})
        else:
            print(f"\n  ⚠  Interrupted before any audio was generated for chapter {chapter_index + 1}.")
        raise StopRequested

    if not all_audio:
        print(f"  ⚠  No audio generated for chapter {chapter_index + 1}.")
        return None

    merged = np.concatenate(all_audio).astype(np.float32)
    output_path = chapter_dir.parent / f"chapter_{chapter_index + 1:03d}.wav"
    sf.write(str(output_path), merged, SAMPLE_RATE)

    duration_min = len(merged) / SAMPLE_RATE / 60
    print(f"  ✓  Saved → {output_path.name}  ({duration_min:.1f} min)")
    if progress_cb:
        progress_cb({"type": "chapter_done", "path": str(output_path), "duration_min": duration_min})
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_chapter_selection(selection: str, total: int) -> list[int]:
    """
    Parse a chapter selection string into a sorted list of 0-based indices.

    Supports:
      "5"         → [4]
      "1,3,5"     → [0, 2, 4]
      "5-10"      → [4, 5, 6, 7, 8, 9]
      "5 - 10"    → [4, 5, 6, 7, 8, 9]   (spaces around dash are fine)
      "1,3,5-8"   → [0, 2, 4, 5, 6, 7]
      "last"      → [total - 1]
    """
    indices = set()
    # Normalise spaces around hyphens so "5 - 10" → "5-10"
    selection = re.sub(r"\s*-\s*", "-", selection)

    for token in selection.split(","):
        token = token.strip()
        if not token:
            continue
        if token.lower() == "last":
            indices.add(total - 1)
        elif "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0].strip())
                end = int(parts[1].strip())
            except ValueError:
                raise ValueError(f"Invalid range: '{token}'. Use format like '5-10'.")
            if start < 1 or end > total or start > end:
                raise ValueError(
                    f"Range {start}-{end} is out of bounds (book has {total} chapters)."
                )
            indices.update(range(start - 1, end))
        else:
            try:
                n = int(token)
            except ValueError:
                raise ValueError(f"Invalid chapter number: '{token}'.")
            if n < 1 or n > total:
                raise ValueError(f"Chapter {n} is out of bounds (book has {total} chapters).")
            indices.add(n - 1)

    return sorted(indices)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert an EPUB to chapter-wise audiobook WAVs using Kokoro TTS."
    )
    parser.add_argument("epub", nargs="?", help="Path to the .epub file")
    parser.add_argument(
        "--voice", default="am_adam",
        help=f"Kokoro voice ID (default: am_adam). Use --list-voices to see options."
    )
    parser.add_argument(
        "--lang", default="a",
        help="Kokoro language code: 'a' = American English, 'b' = British English (default: a)"
    )
    parser.add_argument(
        "--output", default="./audiobook",
        help="Output directory (default: ./audiobook)"
    )
    parser.add_argument(
        "--chapters", default=None,
        help=(
            "Chapters to process. Supports individual numbers, ranges, or a mix. "
            "Examples: '5'  '1,3,5'  '5-10'  '5 - 10'  '1,3,5-8'  'last'  (default: all)"
        )
    )
    parser.add_argument(
        "--keep-segments", action="store_true",
        help="Keep individual TTS segment WAVs (useful for debugging)"
    )
    parser.add_argument(
        "--list-voices", action="store_true",
        help="Print available voice IDs and exit"
    )
    parser.add_argument(
        "--list-chapters", action="store_true",
        help="Print chapter list from EPUB and exit (no TTS)"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress paragraph-level progress output"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_voices:
        print("Available Kokoro voices:")
        for v in AVAILABLE_VOICES:
            print(f"  {v}")
        return

    if not args.epub:
        print("Error: please provide a path to an .epub file.")
        sys.exit(1)

    epub_path = Path(args.epub)
    if not epub_path.exists():
        print(f"Error: file not found: {epub_path}")
        sys.exit(1)

    print(f"📖 Parsing EPUB: {epub_path.name}")
    chapters = get_chapters(str(epub_path))
    print(f"   Found {len(chapters)} chapter(s).")

    if args.list_chapters:
        for i, ch in enumerate(chapters):
            print(f"  [{i + 1:03d}] {ch['title']}  ({ch['word_count']:,} words)")
        return

    # Filter chapters if requested
    if args.chapters:
        try:
            indices = parse_chapter_selection(args.chapters, len(chapters))
        except ValueError as e:
            print(f"Error in --chapters: {e}")
            sys.exit(1)
        chapters = [(i, chapters[i]) for i in indices]
    else:
        chapters = list(enumerate(chapters))

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🎙  Initialising Kokoro TTS  (lang={args.lang}, voice={args.voice})")
    pipeline = KPipeline(lang_code=args.lang)

    print(f"🔊 Synthesising {len(chapters)} chapter(s) → {output_dir}/\n")

    try:
        for chapter_index, chapter in chapters:
            title = chapter["title"]
            word_count = chapter["word_count"]
            print(f"── Chapter {chapter_index + 1}: {title}  ({word_count:,} words)")

            chapter_dir = output_dir / f"chapter_{chapter_index + 1:03d}_segments"
            try:
                output_path = synthesise_chapter(
                    pipeline=pipeline,
                    text=chapter["text"],
                    voice=args.voice,
                    chapter_dir=chapter_dir,
                    chapter_index=chapter_index,
                    verbose=not args.quiet,
                )
            except StopRequested:
                raise  # bubble up to outer handler

            # Clean up segment files unless --keep-segments
            if not args.keep_segments and chapter_dir.exists():
                for seg_file in chapter_dir.glob("seg_*.wav"):
                    seg_file.unlink()
                try:
                    chapter_dir.rmdir()
                except OSError:
                    pass  # not empty, leave it

    except (KeyboardInterrupt, StopRequested):
        print(f"\n🛑  Stopped. Any completed chapters have been saved to: {output_dir.resolve()}")
        sys.exit(0)

    print(f"\n✅ Done! Audio files saved to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
