"""Minimal .docx writer (zipfile + xml only). Blocks in, valid Word file out.

Block kinds: ("title", text) ("h2", text) ("p", text) ("bullet", text)
("table", rows, col_weights|None) ("small", text). Text supports **bold** runs.
"""
import re
import zipfile
from xml.sax.saxutils import escape

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MARGIN = 1008  # 0.7 in in twips
PAGE_W, PAGE_H = 12240, 15840


def _t(s):
    return escape(_CTRL.sub("", str(s)))


def _runs(text, size=None, bold_all=False):
    out = []
    for i, part in enumerate(str(text).split("**")):
        if part == "":
            continue
        props = ""
        if bold_all or i % 2 == 1:
            props += "<w:b/>"
        if size:
            props += '<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size, size)
        rpr = "<w:rPr>%s</w:rPr>" % props if props else ""
        out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, _t(part)))
    return "".join(out)


def _para(text, style=None, ppr_extra="", size=None, bold_all=False):
    ppr = ""
    if style:
        ppr += '<w:pStyle w:val="%s"/>' % style
    ppr += ppr_extra
    ppr = "<w:pPr>%s</w:pPr>" % ppr if ppr else ""
    return "<w:p>%s%s</w:p>" % (ppr, _runs(text, size, bold_all))


def _table(rows, weights=None):
    ncol = max(len(r) for r in rows)
    total = PAGE_W - 2 * MARGIN
    weights = weights or [1] * ncol
    s = float(sum(weights))
    widths = [int(total * w / s) for w in weights]
    b = "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="808080"/>' % e
                for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
    x = ['<w:tbl><w:tblPr><w:tblW w:w="%d" w:type="dxa"/><w:tblBorders>%s</w:tblBorders>'
         '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="60" w:type="dxa"/>'
         '<w:right w:w="60" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>%s</w:tblGrid>'
         % (total, b, "".join('<w:gridCol w:w="%d"/>' % w for w in widths))]
    for ri, row in enumerate(rows):
        x.append("<w:tr>")
        for ci in range(ncol):
            cell = row[ci] if ci < len(row) else ""
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if ri == 0 else ""
            x.append('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr>%s</w:tc>'
                     % (widths[ci], shade,
                        _para(cell, "TableText", bold_all=(ri == 0))))
        x.append("</w:tr>")
    x.append("</w:tbl>")
    return "".join(x)


STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles %s>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="40" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="100" w:after="40"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/><w:szCs w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Bullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="284" w:hanging="200"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0"/></w:pPr><w:rPr><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Small"><w:name w:val="Small"/><w:basedOn w:val="Normal"/><w:rPr><w:i/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr></w:style>
</w:styles>""" % NS

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def document_xml(blocks):
    body = []
    for blk in blocks:
        k = blk[0]
        if k == "title":
            body.append(_para(blk[1], "Title"))
        elif k == "h2":
            body.append(_para(blk[1], "Heading2"))
        elif k == "bullet":
            body.append(_para("• " + blk[1], "Bullet"))
        elif k == "small":
            body.append(_para(blk[1], "Small"))
        elif k == "table":
            body.append(_table(blk[1], blk[2] if len(blk) > 2 else None))
            body.append(_para("", ppr_extra='<w:spacing w:after="0" w:line="120" w:lineRule="exact"/>'))
        else:
            body.append(_para(blk[1]))
    sect = ('<w:sectPr><w:pgSz w:w="%d" w:h="%d"/><w:pgMar w:top="%d" w:right="%d" '
            'w:bottom="%d" w:left="%d" w:header="360" w:footer="360" w:gutter="0"/></w:sectPr>'
            % (PAGE_W, PAGE_H, MARGIN, MARGIN, MARGIN, MARGIN))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document %s><w:body>%s%s'
            '</w:body></w:document>' % (NS, "".join(body), sect))


def write_docx(path, blocks):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/document.xml", document_xml(blocks))
    return path
