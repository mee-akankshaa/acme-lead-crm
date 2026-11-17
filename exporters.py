"""Dependency-free Excel (.xlsx) and PDF writers for the Acme Leads sales report.

The lead-app is deliberately pure Python stdlib (no pip packages, no external
tools) so it keeps running without any tooling around it - these two writers
keep to that rule: .xlsx is a zip of hand-written XML, the PDF is hand-written
PDF 1.4 using the built-in Helvetica fonts (WinAnsi text only; Excel keeps full Unicode).
"""
import datetime
import io
import re
import zlib
import zipfile
from xml.sax.saxutils import escape

# ---------------------------------------------------------------- XLSX ----
# cell spec: plain value, or (value, style) with style in
#   "title" | "sub" | "head" | "bold" | "money" | "moneyb" | "note"
_STYLE_IDX = {None: 0, "title": 1, "sub": 2, "head": 3, "bold": 4, "money": 5, "moneyb": 6, "note": 7}

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_xml(ref, spec):
    style = None
    val = spec
    if isinstance(spec, tuple):
        val, style = spec
    sid = _STYLE_IDX.get(style, 0)
    if val is None or val == "":
        return f'<c r="{ref}" s="{sid}"/>' if sid else ""
    if isinstance(val, bool):
        val = str(val)
    if isinstance(val, (int, float)):
        return f'<c r="{ref}" s="{sid}"><v>{val}</v></c>'
    t = escape(_CTRL.sub("", str(val)))
    return f'<c r="{ref}" s="{sid}" t="inlineStr"><is><t xml:space="preserve">{t}</t></is></c>'


def _sheet_xml(rows, widths, freeze_row):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if freeze_row:
        out.append(f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze_row}" topLeftCell="A{freeze_row + 1}" '
                   f'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    if widths:
        out.append("<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_col_letter(c)}{r}", spec) for c, spec in enumerate(row))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0"/></numFmts>
<fonts count="5">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="15"/><name val="Calibri"/></font>
<font><sz val="10"/><color rgb="FF666666"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF2F6BFF"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="4" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def make_xlsx(sheets):
    """sheets: list of dicts {name, rows, widths, freeze} -> bytes of a .xlsx file."""
    names = []
    for s in sheets:
        n = re.sub(r"[\[\]\*\?/\\:]", "-", s["name"])[:31] or "Sheet"
        names.append(n)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets)))
                   + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", _STYLES)
        for i, s in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(s["rows"], s.get("widths"), s.get("freeze", 0)))
    return buf.getvalue()


# ----------------------------------------------------------------- PDF ----
# Helvetica advance widths (1/1000 em) for ASCII 32..126
_HW = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
       556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
       1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
       667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
       333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
       556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def _clean(s):
    s = "" if s is None else str(s)
    s = s.replace("–", "-").replace("—", "-").replace("‘", "'") \
         .replace("’", "'").replace("“", '"').replace("”", '"').replace("×", "x") \
         .replace("•", "-").replace("→", "->").replace("\n", " ")
    return s.encode("cp1252", "replace").decode("cp1252")


def _tw(s, size, bold=False):
    w = 0
    for ch in s:
        o = ord(ch)
        w += _HW[o - 32] if 32 <= o <= 126 else 556
    return w * size / 1000.0 * (1.07 if bold else 1.0)


def _wrap(text, width, size, bold=False):
    text = _clean(text)
    lines, cur = [], ""
    for word in text.split(" "):
        while _tw(word, size, bold) > width and len(word) > 1:   # break very long tokens
            k = len(word)
            while k > 1 and _tw(word[:k], size, bold) > width:
                k -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:k])
            word = word[k:]
        trial = (cur + " " + word).strip() if cur else word
        if _tw(trial, size, bold) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    lines.append(cur)
    return lines or [""]


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _PDF:
    W, H, M = 595.0, 842.0, 36.0

    def __init__(self, title, subtitle=""):
        self.title, self.subtitle = _clean(title), _clean(subtitle)
        self.pages = []
        self.ops = None
        self.y = 0
        self._new_page()
        self._head()

    def _new_page(self):
        self.ops = []
        self.pages.append(self.ops)
        self.y = self.H - self.M

    def _text(self, x, y, s, size=9, bold=False, color=(0.1, 0.12, 0.16)):
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {color[0]} {color[1]} {color[2]} rg "
                        f"{x:.2f} {y:.2f} Td ({_esc(_clean(s))}) Tj ET")

    def _rect(self, x, y, w, h, fill=None, stroke=None):
        if fill:
            self.ops.append(f"{fill[0]} {fill[1]} {fill[2]} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
        if stroke:
            self.ops.append(f"{stroke[0]} {stroke[1]} {stroke[2]} RG 0.5 w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def _head(self):
        self._text(self.M, self.y - 16, self.title, 16, True, (0.05, 0.07, 0.1))
        self.y -= 26
        if self.subtitle:
            for ln in _wrap(self.subtitle, self.W - 2 * self.M, 9):
                self._text(self.M, self.y - 9, ln, 9, False, (0.4, 0.43, 0.48))
                self.y -= 12
        self.y -= 8

    def heading(self, s, size=12):
        if self.y < self.M + 60:
            self._new_page()
        self._text(self.M, self.y - size, s, size, True, (0.05, 0.07, 0.1))
        self.y -= size + 8

    def para(self, s, size=9, color=(0.25, 0.28, 0.33), bold=False):
        for ln in _wrap(s, self.W - 2 * self.M, size, bold):
            if self.y < self.M + 20:
                self._new_page()
            self._text(self.M, self.y - size, ln, size, bold, color)
            self.y -= size + 4
        self.y -= 4

    def table(self, headers, rows, ratios, aligns=None, bold_last=False, size=8.5):
        usable = self.W - 2 * self.M
        tot = float(sum(ratios))
        widths = [usable * r / tot for r in ratios]
        aligns = aligns or ["l"] * len(headers)
        pad = 4
        lead = size + 3

        def draw_row(cells, fill=None, bold=False, hdr=False):
            wrapped = [_wrap(c, widths[i] - 2 * pad, size, bold or hdr) for i, c in enumerate(cells)]
            h = max(len(w) for w in wrapped) * lead + 2 * pad - 2
            if self.y - h < self.M + 18:
                self._new_page()
                draw_row(headers, hdr=True)
            top = self.y
            x = self.M
            for i, w in enumerate(wrapped):
                self._rect(x, top - h, widths[i], h, fill=(0.18, 0.42, 1.0) if hdr else fill, stroke=(0.82, 0.85, 0.9))
                for j, ln in enumerate(w):
                    tx = x + pad
                    tw = _tw(ln, size, bold or hdr)
                    if aligns[i] == "r":
                        tx = x + widths[i] - pad - tw
                    elif aligns[i] == "c":
                        tx = x + (widths[i] - tw) / 2
                    self._text(tx, top - pad - size - j * lead + 1, ln, size, bold or hdr,
                               (1, 1, 1) if hdr else (0.1, 0.12, 0.16))
                x += widths[i]
            self.y -= h

        draw_row(headers, hdr=True)
        for k, r in enumerate(rows):
            last = bold_last and k == len(rows) - 1
            draw_row(r, fill=(0.93, 0.95, 0.99) if last else None, bold=last)
        self.y -= 12

    def render(self):
        n = len(self.pages)
        stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
        objs = []

        def add(b):
            objs.append(b)
            return len(objs)

        add(b"<< /Type /Catalog /Pages 2 0 R >>")
        add(b"")  # pages placeholder (obj 2)
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for i, ops in enumerate(self.pages):
            foot = (f"BT /F1 8 Tf 0.45 0.48 0.53 rg {self.M:.2f} 20 Td "
                    f"({_esc(_clean('Acme Leads - generated ' + stamp))}) Tj ET "
                    f"BT /F1 8 Tf 0.45 0.48 0.53 rg {self.W - self.M - 50:.2f} 20 Td (Page {i + 1} of {n}) Tj ET")
            stream = zlib.compress(("\n".join(ops) + "\n" + foot).encode("cp1252", "replace"))
            c = add(b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            p = add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W:.0f} {self.H:.0f}] "
                    f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {c} 0 R >>".encode())
            kids.append(p)
        objs[1] = ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode()
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offs = []
        for i, o in enumerate(objs, start=1):
            offs.append(out.tell())
            out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
        for o in offs:
            out.write(f"{o:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return out.getvalue()


def make_pdf(title, subtitle, blocks):
    """blocks: ("heading", text) | ("para", text) | ("table", headers, rows, ratios, aligns, bold_last)."""
    pdf = _PDF(title, subtitle)
    for b in blocks:
        if b[0] == "heading":
            pdf.heading(b[1])
        elif b[0] == "para":
            pdf.para(b[1])
        elif b[0] == "table":
            pdf.table(b[1], b[2], b[3], b[4] if len(b) > 4 else None, b[5] if len(b) > 5 else False)
    return pdf.render()
