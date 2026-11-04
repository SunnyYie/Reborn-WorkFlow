import unittest

from lxml import etree as ET
from template_renderer import TemplateRenderer, NS, q, layout_errors


class CompactStructureTableTests(unittest.TestCase):
    def tables(self, route):
        renderer = TemplateRenderer()
        renderer.table({"route": route})
        return renderer.body.findall("w:tbl", NS)

    def test_content_width_and_equal_rows(self):
        table, = self.tables([
            {"label": "IN"}, {"label": "V1"},
            {"label": "IN", "current_cue": "16 祷告"}, {"label": "C"}])
        widths = [int(c.get(q("w"))) for c in table.findall("w:tblGrid/w:gridCol", NS)]
        self.assertEqual(widths, [600, 600, 1070, 600])
        self.assertEqual(table.find("w:tblPr/w:tblW", NS).get(q("w")), str(sum(widths)))
        self.assertLess(sum(widths), 8300)
        heights = table.findall("w:tr/w:trPr/w:trHeight", NS)
        self.assertEqual([h.attrib for h in heights],
                         [{q("val"): "440", q("hRule"): "exact"}] * 2)
        for cell in table.findall("w:tr/w:tc", NS):
            self.assertEqual(cell.find("w:tcPr/w:vAlign", NS).get(q("val")), "center")
            self.assertEqual(cell.find("w:p/w:pPr/w:spacing", NS).get(q("lineRule")), "exact")
        self.assertIn("16 祷告", table.xpath(".//w:t/text()", namespaces=NS))

    def test_multiline_cue_expands_both_rows(self):
        table, = self.tables([{"label": "IN", "current_cue": "8 话语\n看提示进入"}])
        self.assertEqual([h.get(q("val")) for h in table.findall("w:tr/w:trPr/w:trHeight", NS)],
                         ["648", "648"])
        self.assertEqual(len(table.findall(".//w:br", NS)), 1)

    def test_long_route_wraps_without_losing_order(self):
        route = [{"label": "IN", "current_cue": "本次明确的现场提示"} for _ in range(15)]
        tables = self.tables(route)
        self.assertGreater(len(tables), 1)
        self.assertEqual(sum(len(t.findall("w:tr", NS)[0].findall("w:tc", NS))
                             for t in tables), len(route))
        for table in tables:
            self.assertLessEqual(int(table.find("w:tblPr/w:tblW", NS).get(q("w"))), 8300)
            heights = table.findall("w:tr/w:trPr/w:trHeight", NS)
            self.assertEqual(heights[0].attrib, heights[1].attrib)

    def test_instrument_labels_normalized_without_mutating_source(self):
        route = [{"label": label, "current_cue": str(i)}
                 for i, label in enumerate(("IN", "INT", "Ins", "Int"))]
        table, = self.tables(route)
        self.assertEqual(table.findall("w:tr", NS)[0].xpath(".//w:t/text()", namespaces=NS),
                         ["IN"] * 4)
        self.assertEqual(table.findall("w:tr", NS)[1].xpath(".//w:t/text()", namespaces=NS),
                         ["0", "1", "2", "3"])
        self.assertEqual([x["label"] for x in route], ["IN", "INT", "Ins", "Int"])
        self.assertEqual(TemplateRenderer.instrument_notation("Bridge前8小节Int，不是4小节Ins。"),
                         "Bridge前8小节IN，不是4小节IN。")
        self.assertEqual(TemplateRenderer.instrument_notation("INTRO INTERLUDE INT1 in"),
                         "INTRO INTERLUDE INT1 in")

    def test_current_cue_instrument_notation_is_normalized(self):
        route = [{"label": "Int", "current_cue": "8小节Int，前Ins不说话"}]
        table, = self.tables(route)
        self.assertEqual(table.findall("w:tr", NS)[1].xpath(".//w:t/text()", namespaces=NS),
                         ["8小节IN，前IN不说话"])
        self.assertEqual(route[0]["current_cue"], "8小节Int，前Ins不说话")

    def test_structural_validator_catches_width_height_alignment_and_notation(self):
        for change in ("width", "height", "alignment", "notation", "row_count"):
            renderer = TemplateRenderer()
            renderer.table({"route": [{"label": "IN"}, {"label": "C"}]})
            table = renderer.body.find("w:tbl", NS)
            if change == "width":
                table.find("w:tblPr/w:tblW", NS).set(q("w"), "8300")
            elif change == "height":
                table.find("w:tr/w:trPr/w:trHeight", NS).set(q("val"), "900")
            elif change == "alignment":
                table.find("w:tr/w:tc/w:tcPr/w:vAlign", NS).set(q("val"), "top")
            elif change == "notation":
                table.find("w:tr/w:tc/w:p/w:r/w:t", NS).text = "Int"
            else:
                table.remove(table.findall("w:tr", NS)[-1])
            with self.subTest(change=change):
                self.assertTrue(layout_errors(renderer.root))

    def test_save_checks_before_creating_invalid_document(self):
        import tempfile
        from pathlib import Path
        renderer = TemplateRenderer()
        renderer.table({"route": [{"label": "C"}]})
        renderer.body.find("w:tbl/w:tblPr/w:tblW", NS).set(q("w"), "8300")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.docx"
            with self.assertRaises(ValueError):
                renderer.save(path)
            self.assertFalse(path.exists())

    def test_main_table_widths_are_balanced_but_ending_stays_compact(self):
        renderer = TemplateRenderer()
        for number, count in ((1, 9), (2, 7), (3, 10)):
            renderer.write(f"{number}. song", "C00000", 16, True, new_page=number > 1)
            renderer.table({"route": [{"label": "V1"}] * count})
        renderer.write("回应诗歌：song", "C00000", 14, True, new_page=True)
        renderer.write("结束诗歌：song", "C00000", 14, True)
        renderer.table({"route": [{"label": "V1"}] * 5})
        tables = renderer.body.findall("w:tbl", NS)
        self.assertEqual([int(t.find("w:tblPr/w:tblW", NS).get(q("w"))) for t in tables],
                         [6000, 6000, 6000, 3000])
        for table in tables:
            self.assertEqual(table.find("w:tblPr/w:tblInd", NS).get(q("w")), "120")
            self.assertEqual(table.find("w:tblPr/w:jc", NS).get(q("val")), "left")
        self.assertFalse(layout_errors(renderer.root))
        tables[0].find("w:tblGrid/w:gridCol", NS).set(q("w"), "600")
        self.assertTrue(layout_errors(renderer.root))


class LyricColumnTests(unittest.TestCase):
    def test_all_adjacent_sections_have_gap_but_column_starts_do_not(self):
        renderer = TemplateRenderer()
        renderer.lyrics({"lyric_sections": [
            {"label": label, "lines": ["first lyric", "last lyric"]}
            for label in ("V1", "V2", "C", "PC", "B", "TAG")]})
        self.assertFalse(layout_errors(renderer.root))
        for text in ("Verse 1", "Verse 2", "Chorus", "Pre Chorus", "Bridge", "Tag"):
            heading = renderer.body.xpath("./w:p[w:r/w:t=$text]", namespaces=NS, text=text)[0]
            self.assertEqual(heading.find("w:pPr/w:spacing", NS).get(q("before")),
                             "0" if text in ("Verse 1", "Chorus") else "240")
            self.assertIsNotNone(heading.find("w:pPr/w:keepNext", NS))
        self.assertEqual(renderer.body.xpath(".//w:t/text()", namespaces=NS).count("first lyric"), 6)

    def test_missing_section_gap_or_extra_column_start_gap_is_rejected(self):
        for text, wrong in (("Verse 2", "0"), ("Bridge", "30"), ("Chorus", "240")):
            renderer = TemplateRenderer()
            renderer.lyrics({"lyric_sections": [
                {"label": label, "lines": ["lyric words"]} for label in ("V1", "V2", "C", "B")]})
            self.assertFalse(layout_errors(renderer.root))
            heading = renderer.body.xpath("./w:p[w:r/w:t=$text]", namespaces=NS, text=text)[0]
            heading.find("w:pPr/w:spacing", NS).set(q("before"), wrong)
            with self.subTest(text=text):
                self.assertTrue(layout_errors(renderer.root))

    def test_left_edges_and_both_column_top_gaps_are_enforced(self):
        for change in ("title", "scripture", "lyric", "table", "gap", "right_gap"):
            renderer = TemplateRenderer()
            title = renderer.write("1. song", "C00000", 16, True)
            scripture = renderer.write("【经文】scripture", role="scripture")
            renderer.table({"route": [{"label": "V1"}, {"label": "C"}]})
            renderer.lyrics({"lyric_sections": [
                {"label": "V1", "lines": ["verse words"]}, {"label": "C", "lines": ["chorus words"]}]})
            from template_renderer import style_errors
            self.assertFalse(style_errors(renderer.root))
            headings = [renderer.body.xpath("./w:p[w:r/w:t=$text]", namespaces=NS, text=text)[0]
                        for text in ("Verse 1", "Chorus")]
            for heading in headings:
                self.assertEqual(heading.find("w:pPr/w:spacing", NS).get(q("before")), "0")
            if change == "gap":
                boundary = renderer.body.xpath("./w:p[w:pPr/w:sectPr/w:cols[@w:num='1']]", namespaces=NS)[0]
                boundary.getprevious().find("w:pPr/w:spacing", NS).set(q("line"), "0")
            elif change == "right_gap":
                headings[1].find("w:pPr/w:spacing", NS).set(q("before"), "180")
            elif change == "table":
                renderer.body.find("w:tbl/w:tblPr/w:tblInd", NS).set(q("w"), "0")
            else:
                paragraph = title if change == "title" else scripture if change == "scripture" else headings[0]
                paragraph.find("w:pPr/w:ind", NS).set(q("left"), "120")
            with self.subTest(change=change):
                self.assertTrue(layout_errors(renderer.root))

    def test_column_break_starts_chorus_heading_without_blank_paragraph(self):
        renderer = TemplateRenderer()
        renderer.lyrics({"lyric_sections": [
            {"label": "V1", "lines": ["verse one"]},
            {"label": "V2", "lines": ["verse two"]},
            {"label": "C", "lines": ["chorus"]},
            {"label": "B", "lines": ["bridge"]},
        ]})
        breaks = renderer.body.xpath(".//w:br[@w:type='column']", namespaces=NS)
        self.assertEqual(len(breaks), 1)
        paragraph = breaks[0].getparent().getparent()
        self.assertEqual(paragraph.xpath(".//w:t/text()", namespaces=NS), ["Chorus"])
        self.assertEqual(paragraph.find("w:r", NS)[1].tag, q("br"))
        for text in ("Verse 1", "Chorus"):
            heading, = renderer.body.xpath(
                "./w:p[w:r/w:t=$text]", namespaces=NS, text=text)
            self.assertEqual(heading.find("w:pPr/w:spacing", NS).get(q("after")), "90")
        self.assertEqual(renderer.body.xpath(".//w:t/text()", namespaces=NS),
                         ["Verse 1", "verse one", "Verse 2", "verse two",
                          "Chorus", "chorus", "Bridge", "bridge"])
        self.assertEqual(len(renderer.body.findall(".//w:sectPr", NS)), 2)

    def test_single_sided_lyrics_have_no_column_break(self):
        for label in ("V1", "C"):
            renderer = TemplateRenderer()
            renderer.lyrics({"lyric_sections": [{"label": label, "lines": ["lyrics"]}]})
            self.assertFalse(renderer.body.xpath(".//w:br[@w:type='column']", namespaces=NS))

    def test_separate_column_break_or_non_heading_break_is_rejected(self):
        for text in ("", "ordinary lyric"):
            renderer = TemplateRenderer()
            paragraph = renderer.write(text)
            ET.SubElement(paragraph.find("w:r", NS), q("br"), {q("type"): "column"})
            self.assertTrue(layout_errors(renderer.root))

    def test_lyric_font_color_size_spacing_and_pagination_are_validated(self):
        for change in ("font", "color", "size", "spacing", "keep_lines", "heading_weight"):
            renderer = TemplateRenderer()
            renderer.lyrics({"lyric_sections": [
                {"label": "V1", "lines": ["第一行歌词", "第二行歌词"]},
                {"label": "C", "lines": ["副歌歌词"]}]})
            self.assertFalse(layout_errors(renderer.root))
            heading = renderer.body.xpath("./w:p[w:r/w:t='Verse 1']", namespaces=NS)[0]
            lyric = renderer.body.xpath("./w:p[w:r/w:t='第一行歌词']", namespaces=NS)[0]
            if change == "font":
                lyric.find("w:r/w:rPr/w:rFonts", NS).set(q("eastAsia"), "Wrong font")
            elif change == "color":
                lyric.find("w:r/w:rPr/w:color", NS).set(q("val"), "C55A11")
            elif change == "size":
                lyric.find("w:r/w:rPr/w:sz", NS).set(q("val"), "16")
            elif change == "spacing":
                lyric.find("w:pPr/w:spacing", NS).set(q("after"), "800")
            elif change == "keep_lines":
                pp = lyric.find("w:pPr", NS)
                pp.remove(pp.find("w:keepLines", NS))
            else:
                rp = heading.find("w:r/w:rPr", NS)
                rp.remove(rp.find("w:b", NS))
            with self.subTest(change=change):
                self.assertTrue(layout_errors(renderer.root))

    def test_each_stanza_stays_together_without_linking_columns(self):
        renderer = TemplateRenderer()
        renderer.lyrics({"lyric_sections": [
            {"label": "V1", "lines": ["verse first", "verse last"]},
            {"label": "C", "lines": ["chorus first", "chorus last"]}]})
        for text in ("Verse 1", "verse first", "Chorus", "chorus first"):
            paragraph = renderer.body.xpath("./w:p[w:r/w:t=$text]", namespaces=NS, text=text)[0]
            self.assertIsNotNone(paragraph.find("w:pPr/w:keepNext", NS))
        for text in ("verse last", "chorus last"):
            paragraph = renderer.body.xpath("./w:p[w:r/w:t=$text]", namespaces=NS, text=text)[0]
            self.assertIsNone(paragraph.find("w:pPr/w:keepNext", NS))

    def test_table_palette_fonts_padding_and_exact_height_are_validated(self):
        for change in ("ink", "fill", "font", "padding", "height"):
            renderer = TemplateRenderer()
            renderer.table({"route": [{"label": "V1"}, {"label": "C"}]})
            self.assertFalse(layout_errors(renderer.root))
            table = renderer.body.find("w:tbl", NS)
            if change == "ink":
                table.find("w:tr/w:tc/w:p/w:r/w:rPr/w:color", NS).set(q("val"), "000000")
            elif change == "fill":
                table.find("w:tr/w:tc/w:tcPr/w:shd", NS).set(q("fill"), "FFFFFF")
            elif change == "font":
                table.find("w:tr/w:tc/w:p/w:r/w:rPr/w:rFonts", NS).set(q("ascii"), "Wrong")
            elif change == "padding":
                table.find("w:tblPr/w:tblCellMar/w:top", NS).set(q("w"), "500")
            else:
                for h in table.findall("w:tr/w:trPr/w:trHeight", NS):
                    h.set(q("val"), "900")
            with self.subTest(change=change):
                self.assertTrue(layout_errors(renderer.root))

    def test_missing_fresh_page_boundaries_cannot_pass_structural_check(self):
        for change in ("main", "response", "ending"):
            renderer = TemplateRenderer()
            for i in (1, 2, 3):
                renderer.write(f"{i}. test song", "C00000", 16, True,
                               new_page=i > 1 and not (change == "main" and i == 2))
            renderer.write("回应诗歌：test", "C00000", 14, True, new_page=change != "response")
            renderer.write("结束诗歌：test", "C00000", 14, True, new_page=change == "ending")
            with self.subTest(change=change):
                self.assertTrue(layout_errors(renderer.root))


if __name__ == "__main__":
    unittest.main()
