"""Template-backed setlist components; acquisition and validation live in rag.py."""

from copy import deepcopy
from pathlib import Path
import re
import math
import unicodedata
import zipfile
from lxml import etree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W}
LAYOUT_POLICY_VERSION = 5
MAIN_TABLE_MIN_WIDTH = 6000
LYRIC_TOP_GAP = 180
SECTION_GAP = 240


def q(name):
    return "{" + W + "}" + name


def text_width(text):
    return sum(0 if unicodedata.combining(c) else
               220 if unicodedata.east_asian_width(c) in ("W", "F") else 130 for c in text)


def paragraph_text(node):
    return "".join((child.text or "") if child.tag == q("t") else
                   "\n" if child.tag == q("br") and child.get(q("type")) is None else ""
                   for child in node.iter())


def column_widths(texts, minimum=0):
    widths = [max(600, min(1800, max(text_width(line) for text in pair
                                    for line in text.split("\n")) + 240)) for pair in texts]
    remaining = max(0, min(minimum, len(widths) * 1800) - sum(widths))
    while remaining:
        available = [i for i, width in enumerate(widths) if width < 1800]
        share, remainder = divmod(remaining, len(available))
        for position, i in enumerate(available):
            extra = min(1800 - widths[i], share + (position < remainder))
            widths[i] += extra
            remaining -= extra
    return widths


def table_minimum(table):
    return preceding_table_minimum(table.itersiblings(preceding=True))


def preceding_table_minimum(siblings):
    for sibling in siblings:
        text = paragraph_text(sibling)
        if text.startswith(("回应诗歌：", "结束诗歌：")):
            return 0
        if re.match(r"^[123]\. ", text):
            return MAIN_TABLE_MIN_WIDTH
    return 0


def left_edge_errors(p):
    align, indent = p.find("w:pPr/w:jc", NS), p.find("w:pPr/w:ind", NS)
    if (align is None or align.get(q("val")) != "left" or indent is None
            or any(indent.get(q(k)) != "0" for k in ("left", "right", "firstLine"))
            or any(indent.get(q(k)) not in (None, "0")
                   for k in ("hanging", "leftChars", "rightChars", "firstLineChars", "hangingChars"))):
        return ["text must be explicitly left aligned without indentation"]
    return []


def paragraph_errors(p, font, size, color, bold=False, underline=False, lyric=False, before=0):
    errors = left_edge_errors(p)
    spacing = p.find("w:pPr/w:spacing", NS)
    expected = {"before": str(before), "after": "90" if bold else "30", "line": "240", "lineRule": "auto"}
    if spacing is None or any(spacing.get(q(k)) != v for k, v in expected.items()):
        errors.append("paragraph/lyric spacing differs from the style policy")
    for run in p.findall("w:r", NS):
        if not run.findall("w:t", NS):
            continue
        fonts = run.find("w:rPr/w:rFonts", NS)
        if fonts is None or any(fonts.get(q(k)) != font for k in ("ascii", "hAnsi", "eastAsia")):
            errors.append("font family differs from the style policy")
        for tag, value in (("sz", str(size)), ("szCs", str(size)), ("color", color)):
            node = run.find("w:rPr/w:" + tag, NS)
            if node is None or node.get(q("val")) != value:
                errors.append("font size/color differs from the style policy")
        actual_bold = run.find("w:rPr/w:b", NS)
        if (actual_bold is not None and actual_bold.get(q("val"), "1") not in {"0", "false"}) != bold:
            errors.append("font weight differs from the style policy")
        actual_under = run.find("w:rPr/w:u", NS)
        if underline and (actual_under is None or actual_under.get(q("val")) != "single"):
            errors.append("section underline is missing")
    if bold and p.find("w:pPr/w:keepNext", NS) is None:
        errors.append("heading must stay with its content")
    if lyric and p.find("w:pPr/w:keepLines", NS) is None:
        errors.append("lyric lines must not split across pages")
    return errors


def section_label(text):
    compact = re.sub(r"[\s_-]+", "", text.upper())
    for name, label in (("PRECHORUS", "PC"), ("CHORUS", "C"), ("VERSE", "V"), ("BRIDGE", "B")):
        if re.fullmatch(name + r"\d*", compact):
            return label + compact[len(name):]
    return compact if re.fullmatch(r"(?:V|C|PC|B|LL|TAG)\d*", compact) else None


def style_errors(root):
    errors, segment = [], []
    body = root.find("w:body", NS)
    if body is None:
        return ["missing document body"]
    for child in body:
        sect = child.find("w:pPr/w:sectPr", NS) if child.tag == q("p") else None
        if sect is not None:
            cols = sect.find("w:cols", NS)
            if cols is not None and cols.get(q("num")) == "1":
                gap = child.getprevious()
                spacing = gap.find("w:pPr/w:spacing", NS) if gap is not None else None
                if (gap is None or paragraph_text(gap) or spacing is None or any(
                        spacing.get(q(k)) != v for k, v in {
                            "before": "0", "after": "0", "line": str(LYRIC_TOP_GAP),
                            "lineRule": "exact"}.items())):
                    errors.append("lyrics need a shared 9pt gap before the two-column section")
            if cols is not None and cols.get(q("num")) == "2":
                color = None
                has_section = False
                for p in segment:
                    if p.tag != q("p") or not paragraph_text(p).strip():
                        continue
                    label = section_label(paragraph_text(p))
                    if p.xpath(".//w:br[@w:type='column']", namespaces=NS):
                        has_section = False
                    before = SECTION_GAP if label and has_section else 0
                    if label:
                        color = TemplateRenderer.palette(label)[0]
                        has_section = True
                    if color is None:
                        errors.append("lyrics must start with a recognized section heading")
                        continue
                    errors.extend(paragraph_errors(p, "黑体", 28, color,
                                                   bold=bool(label), underline=bool(label), lyric=True,
                                                   before=before))
            segment = []
            continue
        segment.append(child)
        if child.tag != q("p"):
            continue
        text = paragraph_text(child)
        if re.match(r"^[123]\. ", text):
            errors.extend(paragraph_errors(child, "微软雅黑", 32, "C00000", bold=True))
        elif text.startswith(("回应诗歌：", "结束诗歌：")):
            errors.extend(paragraph_errors(child, "微软雅黑", 28, "C00000", bold=True))
        elif text.startswith("【经文】"):
            errors.extend(paragraph_errors(child, "楷体_GB2312", 24, "000000"))
    return errors


def page_plan_errors(root):
    paragraphs = root.findall("w:body/w:p", NS)
    mains = [p for p in paragraphs if re.match(r"^[123]\. ", paragraph_text(p))]
    if not mains:
        return []
    errors = []
    if [paragraph_text(p)[:2] for p in mains] != ["1.", "2.", "3."]:
        errors.append("three ordered main song blocks are required")
    for p in mains[1:]:
        if p.find("w:pPr/w:pageBreakBefore", NS) is None:
            errors.append("each main song must start on a fresh page")
    response = [p for p in paragraphs if paragraph_text(p).startswith("回应诗歌：")]
    ending = [p for p in paragraphs if paragraph_text(p).startswith("结束诗歌：")]
    if len(response) != 1 or len(ending) != 1:
        errors.append("response and ending blocks are required")
    elif (response[0].find("w:pPr/w:pageBreakBefore", NS) is None
          or ending[0].find("w:pPr/w:pageBreakBefore", NS) is not None):
        errors.append("response/ending must start together on a page separate from song three")
    elif mains and not (paragraphs.index(mains[-1]) < paragraphs.index(response[0])
                        < paragraphs.index(ending[0])):
        errors.append("response/ending must follow all main songs in order")
    for p in paragraphs:
        if paragraph_text(p) == "祷告与衔接话语" and p.find("w:pPr/w:pageBreakBefore", NS) is None:
            errors.append("spoken appendix must not share the response/ending page")
    return errors


def layout_errors(root):
    """Check deterministic OOXML invariants, not rendered page geometry."""
    errors = []
    for index, table in enumerate(root.findall(".//w:tbl", NS), 1):
        prefix = f"table {index}: "
        rows = table.findall("w:tr", NS)
        if len(rows) != 2:
            errors.append(prefix + "expected two rows")
            continue
        grid = table.findall("w:tblGrid/w:gridCol", NS)
        widths = [int(c.get(q("w"), "0")) for c in grid]
        declared = table.find("w:tblPr/w:tblW", NS)
        if (not widths or min(widths) < 600 or sum(widths) > 8300 or declared is None
                or declared.get(q("w")) != str(sum(widths))):
            errors.append(prefix + "table and grid widths disagree or exceed bounds")
        layout = table.find("w:tblPr/w:tblLayout", NS)
        if layout is None or layout.get(q("type")) != "fixed":
            errors.append(prefix + "table must use fixed content-sized columns")
        align, indent = table.find("w:tblPr/w:jc", NS), table.find("w:tblPr/w:tblInd", NS)
        if (align is None or align.get(q("val")) != "left" or indent is None
                or indent.get(q("w")) != "120" or indent.get(q("type")) != "dxa"):
            errors.append(prefix + "table outer left edge must align with unindented text")
        for edge, value in (("top", "60"), ("bottom", "60"), ("left", "120"), ("right", "120")):
            margin = table.find(f"w:tblPr/w:tblCellMar/w:{edge}", NS)
            if margin is None or margin.get(q("w")) != value:
                errors.append(prefix + "cell padding differs from policy")
        heights = [r.find("w:trPr/w:trHeight", NS) for r in rows]
        if (any(h is None for h in heights) or
                any(h.get(q("hRule")) != "exact" for h in heights) or
                heights[0].get(q("val")) != heights[1].get(q("val")) or
                int(heights[0].get(q("val"), "0")) < 440):
            errors.append(prefix + "rows must have equal exact heights")
        for row in rows:
            cells = row.findall("w:tc", NS)
            if len(cells) != len(widths):
                errors.append(prefix + "cell count differs from grid")
            for cell, width in zip(cells, widths):
                cell_width = cell.find("w:tcPr/w:tcW", NS)
                align = cell.find("w:tcPr/w:vAlign", NS)
                if (cell_width is None or cell_width.get(q("w")) != str(width)
                        or align is None or align.get(q("val")) != "center"):
                    errors.append(prefix + "cell width/alignment mismatch")
        expected_widths = []
        if all(len(row.findall("w:tc", NS)) == len(widths) for row in rows):
            expected_widths = column_widths([
                [paragraph_text(row.findall("w:tc", NS)[i]) for row in rows]
                for i in range(len(widths))], table_minimum(table))
        for column, width in enumerate(widths):
            if any(len(row.findall("w:tc", NS)) != len(widths) for row in rows):
                break
            cells = [row.findall("w:tc", NS)[column] for row in rows]
            texts = [paragraph_text(cell) for cell in cells]
            if width != expected_widths[column]:
                errors.append(prefix + "column width differs from content and balanced main-table policy")
            color, fill = TemplateRenderer.palette(texts[0])
            for top, cell in zip((True, False), cells):
                shade = cell.find("w:tcPr/w:shd", NS)
                if shade is None or shade.get(q("fill")) != fill:
                    errors.append(prefix + "section cell fill differs from the palette")
                p = cell.find("w:p", NS)
                spacing = p.find("w:pPr/w:spacing", NS)
                align = p.find("w:pPr/w:jc", NS)
                if (spacing is None or any(spacing.get(q(k)) != v for k, v in {
                        "before": "0", "after": "0", "line": "264", "lineRule": "exact"}.items())
                        or align is None or align.get(q("val")) != "center"):
                    errors.append(prefix + "table text spacing/alignment differs from policy")
                for run in p.findall("w:r", NS):
                    fonts = run.find("w:rPr/w:rFonts", NS)
                    ink = run.find("w:rPr/w:color", NS)
                    size = run.find("w:rPr/w:sz", NS)
                    size_cs = run.find("w:rPr/w:szCs", NS)
                    if (fonts is None or fonts.get(q("ascii")) != "Arial"
                            or fonts.get(q("hAnsi")) != "Arial"
                            or fonts.get(q("eastAsia")) != "微软雅黑"
                            or size is None or size.get(q("val")) != "22"
                            or size_cs is None or size_cs.get(q("val")) != "22"
                            or ink is None or ink.get(q("val")) != color
                            or (run.find("w:rPr/w:b", NS) is not None) != top):
                        errors.append(prefix + "table font/size/weight/color differs from policy")
        if widths and all(len(row.findall("w:tc", NS)) == len(widths) for row in rows):
            lines = max(sum(max(1, math.ceil(text_width(line) / (width - 240)))
                            for line in paragraph_text(cell).split("\n"))
                        for row in rows for cell, width in zip(row.findall("w:tc", NS), widths)
                        if width > 240)
            expected_height = max(440, lines * 264 + 120)
            if any(h is None or h.get(q("val")) != str(expected_height) for h in heights):
                errors.append(prefix + "row height is not sized to its content")
        for row in rows:
            if row.find("w:trPr/w:cantSplit", NS) is None:
                errors.append(prefix + "table rows must not split across pages")
        texts = table.xpath(".//w:t/text()", namespaces=NS)
        if any(TemplateRenderer.instrument_notation(text) != text for text in texts):
            errors.append(prefix + "instrument labels must use IN")
    for br in root.xpath(".//w:br[@w:type='column']", namespaces=NS):
        paragraph = br.getparent()
        while paragraph is not None and paragraph.tag != q("p"):
            paragraph = paragraph.getparent()
        if (paragraph is None
                or not "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
                or paragraph.find("w:r/w:rPr/w:u", NS) is None
                or paragraph.find("w:r/w:rPr/w:b", NS) is None):
            errors.append("column break must be inside the right-hand section heading")
    return errors + style_errors(root) + page_plan_errors(root)


def inspect_docx(path):
    with zipfile.ZipFile(path) as source:
        root = ET.fromstring(source.read("word/document.xml"))
    errors = layout_errors(root)
    if errors:
        raise ValueError("; ".join(errors))
    return {"tables": len(root.findall(".//w:tbl", NS)),
            "column_breaks": len(root.xpath(".//w:br[@w:type='column']", namespaces=NS)),
            "layout_policy_version": LAYOUT_POLICY_VERSION,
            "scope": "ooxml_invariants_not_visual_acceptance"}


class TemplateRenderer:
    def __init__(self):
        self.template = (Path(__file__).resolve().parents[1] / "skills" /
                         "pco-assemble-worship-setlist/assets/reference.docx")
        with zipfile.ZipFile(self.template) as src:
            self.parts = {n: src.read(n) for n in src.namelist()}
        self.root = ET.fromstring(self.parts["word/document.xml"])
        self.body = self.root.find("w:body", NS)
        self.section = deepcopy(self.body.find(".//w:sectPr", NS))
        self.table_seed = deepcopy(self.body.find("w:tbl", NS))
        for node in list(self.body):
            self.body.remove(node)

    @staticmethod
    def palette(label):
        label = re.sub(r"[\s_-]+", "", label.strip().upper())
        label = label.replace("PRECHORUS", "PC")
        if label.startswith("V"):
            return "2E75B6", "9CC2E5"
        if label.startswith(("C", "PC")):
            return "C55A11", "F4B083"
        if label.startswith(("B", "LL", "TAG")):
            return "548235", "A8D08D"
        return "000000", "FFFFFF"

    @staticmethod
    def instrument_notation(text):
        return re.sub(r"(?<![A-Za-z0-9_])(?:INT|Ins|Int|IN)(?![A-Za-z0-9_])", "IN", text)

    def write(self, text, color=None, size=12, bold=False, role=None, new_page=False):
        p = ET.SubElement(self.body, q("p"))
        pp = ET.SubElement(p, q("pPr"))
        ET.SubElement(pp, q("jc"), {q("val"): "left"})
        ET.SubElement(pp, q("ind"), {q("left"): "0", q("right"): "0", q("firstLine"): "0"})
        ET.SubElement(pp, q("spacing"), {
            q("before"): "0",
            q("after"): "90" if bold else "30",
            q("line"): "240", q("lineRule"): "auto"})
        if bold:
            ET.SubElement(pp, q("keepNext"))
        if new_page:
            ET.SubElement(pp, q("pageBreakBefore"))
        if role in ("lyric", "section"):
            ET.SubElement(pp, q("keepLines"))
        run = ET.SubElement(p, q("r"))
        rp = ET.SubElement(run, q("rPr"))
        font = ("楷体_GB2312" if role == "scripture" else
                "黑体" if role in ("lyric", "section") else "微软雅黑")
        ET.SubElement(rp, q("rFonts"), {
            q("ascii"): font, q("hAnsi"): font, q("eastAsia"): font})
        ET.SubElement(rp, q("sz"), {q("val"): str(round(size * 2))})
        ET.SubElement(rp, q("szCs"), {q("val"): str(round(size * 2))})
        ET.SubElement(rp, q("color"), {q("val"): color or "000000"})
        if bold:
            ET.SubElement(rp, q("b"))
        if role in ("section", "scripture") and bold:
            ET.SubElement(rp, q("u"), {q("val"): "single"})
        for i, line in enumerate(text.split("\n")):
            if i:
                ET.SubElement(run, q("br"))
            ET.SubElement(run, q("t"), {
                "{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = line
        return p

    def section_end(self, columns, start="continuous", final=False):
        sect = deepcopy(self.section)
        typ = sect.find("w:type", NS)
        if typ is None:
            typ = ET.SubElement(sect, q("type"))
        typ.set(q("val"), start)
        cols = sect.find("w:cols", NS)
        if cols is None:
            cols = ET.SubElement(sect, q("cols"))
        cols.set(q("num"), str(columns))
        cols.set(q("space"), "425")
        for node in list(cols):
            cols.remove(node)
        for node in list(sect):
            if node.tag in (q("headerReference"), q("footerReference")):
                sect.remove(node)
        # The retained template's populated default header is rId3.
        header = ET.Element(q("headerReference"), {
            q("type"): "default", "{" + R + "}id": "rId3"})
        sect.insert(0, header)
        if final:
            self.body.append(sect)
        else:
            ET.SubElement(ET.SubElement(self.body, q("p")), q("pPr")).append(sect)

    def table(self, record):
        route = record["route"]
        if not route:
            raise ValueError("结构表不能为空")
        def cue(item):
            bar = re.match(r"^\s*(\d+)(?:\s|[^\d]|$)", item.get("cue", ""))
            return self.instrument_notation(
                item["current_cue"] if "current_cue" in item else (bar.group(1) + "*" if bar else ""))

        # Size to the displayed content, not to the template's full-page table.
        chunks, chunk, total = [], [], 0
        for item in route:
            item = dict(item, label=self.instrument_notation(item["label"]))
            texts = (item["label"], cue(item))
            width = max(600, min(1800, max(
                text_width(line) for text in texts for line in text.split("\n")) + 240))
            if chunk and (len(chunk) == 12 or total + width > 8300):
                chunks.append(chunk)
                chunk, total = [], 0
            chunk.append((item, width))
            total += width
        chunks.append(chunk)
        for chunk in chunks:
            balanced = column_widths([(item["label"], cue(item)) for item, _ in chunk],
                                     preceding_table_minimum(reversed(self.body)))
            chunk = [(item, width) for (item, _), width in zip(chunk, balanced)]
            t = deepcopy(self.table_seed)
            for row in t.findall("w:tr", NS):
                t.remove(row)
            pr = t.find("w:tblPr", NS)
            layout = pr.find("w:tblLayout", NS)
            if layout is None:
                layout = ET.SubElement(pr, q("tblLayout"))
            layout.set(q("type"), "fixed")
            ET.SubElement(pr, q("jc"), {q("val"): "left"})
            pr.find("w:tblInd", NS).set(q("w"), "120")
            for floating in pr.findall("w:tblpPr", NS):
                pr.remove(floating)
            pr.find("w:tblW", NS).set(q("w"), str(sum(width for _, width in chunk)))
            margins = pr.find("w:tblCellMar", NS)
            for edge, value in (("top", "60"), ("bottom", "60"),
                                ("left", "120"), ("right", "120")):
                margins.find("w:" + edge, NS).set(q("w"), value)
            grid = t.find("w:tblGrid", NS)
            for node in list(grid):
                grid.remove(node)
            for _, width in chunk:
                ET.SubElement(grid, q("gridCol"), {q("w"): str(width)})
            lines = max(sum(max(1, math.ceil(text_width(line) / (width - 240)))
                            for line in text.split("\n"))
                        for item, width in chunk for text in (item["label"], cue(item)))
            height = max(440, lines * 264 + 120)
            for top in (True, False):
                row = ET.SubElement(t, q("tr"))
                row_pr = ET.SubElement(row, q("trPr"))
                ET.SubElement(row_pr, q("cantSplit"))
                ET.SubElement(row_pr, q("trHeight"), {
                    q("val"): str(height), q("hRule"): "exact"})
                for item, width in chunk:
                    cell = ET.SubElement(row, q("tc"))
                    cp = ET.SubElement(cell, q("tcPr"))
                    ET.SubElement(cp, q("tcW"), {q("w"): str(width), q("type"): "dxa"})
                    color, fill = self.palette(item["label"])
                    ET.SubElement(cp, q("shd"), {q("val"): "clear", q("fill"): fill})
                    ET.SubElement(cp, q("vAlign"), {q("val"): "center"})
                    p = ET.SubElement(cell, q("p"))
                    pp = ET.SubElement(p, q("pPr"))
                    if top:
                        ET.SubElement(pp, q("keepNext"))
                    ET.SubElement(pp, q("jc"), {q("val"): "center"})
                    ET.SubElement(pp, q("spacing"), {
                        q("before"): "0", q("after"): "0",
                        q("line"): "264", q("lineRule"): "exact"})
                    r = ET.SubElement(p, q("r"))
                    rp = ET.SubElement(r, q("rPr"))
                    ET.SubElement(rp, q("rFonts"), {
                        q("ascii"): "Arial", q("hAnsi"): "Arial", q("eastAsia"): "微软雅黑"})
                    ET.SubElement(rp, q("sz"), {q("val"): "22"})
                    ET.SubElement(rp, q("szCs"), {q("val"): "22"})
                    ET.SubElement(rp, q("color"), {q("val"): color})
                    if top:
                        ET.SubElement(rp, q("b"))
                    text = item["label"] if top else cue(item)
                    for i, line in enumerate(text.split("\n")):
                        if i:
                            ET.SubElement(r, q("br"))
                        ET.SubElement(r, q("t")).text = line
            self.body.append(t)

    def lyrics(self, record):
        # Shared whitespace avoids WPS treating paragraph-before differently at column starts.
        gap = ET.SubElement(self.body, q("p"))
        pp = ET.SubElement(gap, q("pPr"))
        ET.SubElement(pp, q("spacing"), {
            q("before"): "0", q("after"): "0", q("line"): str(LYRIC_TOP_GAP), q("lineRule"): "exact"})
        ET.SubElement(pp, q("keepNext"))
        self.section_end(1)
        sections = record["lyric_sections"]
        left = [s for s in sections if s["label"].upper().startswith("V")]
        right = [s for s in sections if s not in left]
        for side, items in enumerate((left, right)):
            for index, section in enumerate(items):
                label = section["label"]
                name = {"V": "Verse", "C": "Chorus", "B": "Bridge",
                        "PC": "Pre Chorus", "TAG": "Tag"}.get(label.upper(), label)
                if re.fullmatch(r"V\d+", label.upper()):
                    name = "Verse " + label[1:]
                color = self.palette(label)[0]
                heading = self.write(name, color, 14, True, "section")
                if index:
                    heading.find("w:pPr/w:spacing", NS).set(q("before"), str(SECTION_GAP))
                if side and left and index == 0:
                    # A separate break paragraph leaves an empty line atop the right column.
                    run = heading.find("w:r", NS)
                    run.insert(1, ET.Element(q("br"), {q("type"): "column"}))
                for line in section["lines"]:
                    # Full-width spaces separate paired lyric phrases in PCO files.
                    for phrase in re.split(r"\u3000\s*", line):
                        if phrase.strip():
                            paragraph = self.write(phrase.strip(), color, 14, role="lyric")
                            # Keep a stanza together without chaining the two columns or whole song.
                            previous = paragraph.getprevious()
                            if previous is not None and previous.tag == q("p"):
                                pp = previous.find("w:pPr", NS)
                                if pp.find("w:keepNext", NS) is None:
                                    ET.SubElement(pp, q("keepNext"))
        self.section_end(2)

    def save(self, path):
        self.section_end(1, final=True)
        errors = layout_errors(self.root)
        if errors:
            raise ValueError("; ".join(errors))
        self.parts["word/document.xml"] = ET.tostring(self.root, encoding="utf-8", xml_declaration=True)
        for name in list(self.parts):
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                root = ET.fromstring(self.parts[name])
                texts = root.findall(".//w:t", NS)
                for node in texts:
                    node.text = ""
                if texts and "/header" in name:
                    texts[0].text = "敬拜赞美歌单"
                self.parts[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dest:
            with zipfile.ZipFile(self.template) as src:
                for info in src.infolist():
                    dest.writestr(info, self.parts[info.filename])
