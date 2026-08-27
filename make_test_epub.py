#!/usr/bin/env python3
"""Generate a tiny test EPUB (4 short chapters) for GUI testing."""
from ebooklib import epub

book = epub.EpubBook()
book.set_identifier("test-0001")
book.set_title("Test Voyage")
book.set_language("en")
book.add_author("Kokoro Studio")

chapters = []
texts = {
    "Chapter One": "The lighthouse stood alone on the cliff, its beam sweeping the dark water. Mara checked the oil level and climbed the spiral stairs, her boots ringing on the iron. Below, the storm was gathering, and the radio crackled with voices she did not recognise.",
    "Chapter Two": "At the harbour, the fishers argued about the strange lights on the horizon. Old Tomas said they were just fishing trawlers, but the young sailor shook her head. She had seen something move in the water, something long and slow, turning under the waves.",
    "Chapter Three": "The map was older than the town itself, folded in oilskin in the captain's locker. Its edges were worn soft, and the ink had faded to the colour of rust. One line, drawn in a different hand, pointed not to the sea but to the hill where the old signal station stood.",
    "Chapter Four": "When the fog finally lifted at dawn, the bay was empty except for a single rowboat, rocking gently against the pier. Inside, a lantern still burned, and a logbook lay open, its last entry dated three years ago.",
}
for i, (title, text) in enumerate(texts.items(), 1):
    ch = epub.EpubHtml(title=title, file_name=f"ch{i}.xhtml", lang="en")
    paras = text.split(". ")
    body = f'<h1>{title}</h1>' + "".join(
        f"<p>{p}.</p>" for p in paras
    )
    ch.content = body
    book.add_item(ch)
    chapters.append(ch)

book.toc = tuple(chapters)
book.spine = ["ncx"] + chapters
book.add_item(epub.EpubNcx())
book.add_item(epub.EpubNav())

epub.write_epub("test_book.epub", book)
print("wrote test_book.epub")
