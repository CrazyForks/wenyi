"""PDF/HTML/Markdown ingestion and export integration tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from bs4 import BeautifulSoup

from trans_novel.assemble.writer import assemble
from trans_novel.ingest.segmenter import load_document
from trans_novel.pipeline.runstore import RunStore


_HTML = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Sample</title></head>
<body>
<h1>Chapter One</h1><p>First paragraph.</p>
<h2>Chapter Two</h2><p>Second paragraph.</p>
</body>
</html>
"""


def _set_test_targets(store: RunStore) -> None:
    manifest = store.load_manifest()
    for chapter_info in manifest["chapters"]:
        chapter = store.load_chapter(chapter_info["index"])
        for segment in chapter.segments:
            segment.target = f"译{chapter.index}-{segment.index}"
        store.save_chapter(chapter)


class TestPdfIngest(unittest.TestCase):
    def test_pdf_reuses_companion_html_without_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"not accessed when companion HTML exists")
            with open(pdf_path + ".html", "w", encoding="utf-8") as file:
                file.write(_HTML)

            document = load_document(pdf_path, "en", "zh")

        self.assertEqual(document.title, "sample")
        self.assertEqual(document.fmt, "pdf")
        self.assertEqual(document.source_path, os.path.abspath(pdf_path))
        self.assertEqual(
            [chapter.title for chapter in document.chapters],
            ["Chapter One", "Chapter Two"],
        )
        self.assertTrue(all(chapter.template for chapter in document.chapters))


class TestHtmlAndMarkdownIntegration(unittest.TestCase):
    def test_html_export_has_one_head_and_translated_content(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(_HTML)
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            store.init_from_document(document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "nested", "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        self.assertEqual(len(rendered.find_all("head")), 1)
        assert rendered.title is not None
        self.assertEqual(rendered.title.get_text(), "Sample")
        self.assertIn("译0-0", rendered.get_text())
        self.assertIsNone(rendered.select_one("[data-tn-id]"))

    def test_markdown_levels_survive_html_export(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("# One\n\nFirst.\n\n## Two\n\nSecond.\n")
            document = load_document(source_path, "en", "zh")
            self.assertEqual(
                [chapter.meta["heading_level"] for chapter in document.chapters],
                [1, 2],
            )
            store = RunStore(os.path.join(directory, "state"))
            store.init_from_document(document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        assert rendered.h1 is not None
        assert rendered.h2 is not None
        self.assertEqual(rendered.h1.get_text(), "译0-0")
        self.assertEqual(rendered.h2.get_text(), "译1-0")
        self.assertIn("译0-1", rendered.get_text())

    def test_bilingual_html_includes_source_style(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "plain.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("Original paragraph.\n")
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            store.init_from_document(document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
                bilingual=True,
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        self.assertIsNotNone(rendered.find("style", id="tn-bilingual-style"))
        self.assertIsNotNone(rendered.find("p", class_="tn-source"))

    def test_markdown_without_heading_uses_default_level(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "plain.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("A paragraph without a heading.\n")

            document = load_document(source_path, "en", "zh")

        self.assertEqual(len(document.chapters), 1)
        self.assertEqual(document.chapters[0].meta["heading_level"], 1)
        self.assertEqual(
            document.chapters[0].segments[0].source,
            "A paragraph without a heading.",
        )


if __name__ == "__main__":
    unittest.main()
