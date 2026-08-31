# ============================================================
# Gujarati Document Converter
# Lightweight stdlib-only DOCX / ODT conversion
# ============================================================
from __future__ import annotations

from html import unescape
from pathlib import Path
from typing import Callable, Iterable
from zipfile import ZipFile
import xml.etree.ElementTree as ET

SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".odt"}

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

W_R = f"{{{W_NS}}}r"
W_T = f"{{{W_NS}}}t"
W_DEL_T = f"{{{W_NS}}}delText"
W_RPR = f"{{{W_NS}}}rPr"
W_RFONTS = f"{{{W_NS}}}rFonts"
W_STYLE = f"{{{W_NS}}}style"
W_RSTYLE = f"{{{W_NS}}}rStyle"
W_VAL = f"{{{W_NS}}}val"
W_P = f"{{{W_NS}}}p"
W_PSTYLE = f"{{{W_NS}}}pStyle"
A_R = f"{{{A_NS}}}r"
A_T = f"{{{A_NS}}}t"
A_RPR = f"{{{A_NS}}}rPr"

# Keep the standard prefixes used by Office files when we serialize XML.
ET.register_namespace("w", W_NS)
ET.register_namespace("a", A_NS)
ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
ET.register_namespace("wp", "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing")
ET.register_namespace("mc", "http://schemas.openxmlformats.org/markup-compatibility/2006")
ET.register_namespace("v", "urn:schemas-microsoft-com:vml")
ET.register_namespace("o", "urn:schemas-microsoft-com:office:office")


def _parse(data: bytes):
    # ElementTree is included with every supported Python installation.
    # No lxml/pip dependency is required.
    return ET.fromstring(data)


def _serialize(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _docx_content_parts(names: Iterable[str]) -> list[str]:
    result = []
    for name in names:
        low = name.lower()
        if not low.startswith("word/") or not low.endswith(".xml"):
            continue
        if low in {
            "word/styles.xml", "word/settings.xml", "word/numbering.xml",
            "word/fonttable.xml", "word/theme/theme1.xml",
        }:
            continue
        if any(x in low for x in (
            "document.xml", "header", "footer", "footnotes", "endnotes",
            "comments", "glossary",
        )):
            result.append(name)
    return result


def _read_style_font_map(styles_data: bytes):
    style_fonts = {}
    default_fonts = set()
    try:
        root = _parse(styles_data)
    except Exception:
        return style_fonts, default_fonts

    for node in root.iter(f"{{{W_NS}}}rFonts"):
        parent = None
        # docDefaults are the only defaults we need here; collect them by
        # walking ancestors is awkward in ElementTree, so use the XML path
        # structure directly below the root.
        # We only add fonts found under w:docDefaults.
        pass
    doc_defaults = root.find(f"{{{W_NS}}}docDefaults")
    if doc_defaults is not None:
        for rfonts in doc_defaults.iter(W_RFONTS):
            default_fonts.update(v for v in rfonts.attrib.values() if v)

    raw = {}
    for style in root.iter(W_STYLE):
        sid = style.get(f"{{{W_NS}}}styleId")
        if not sid:
            continue
        fonts = set()
        for rfonts in style.iter(W_RFONTS):
            fonts.update(v for v in rfonts.attrib.values() if v)
        based = style.find(f"{{{W_NS}}}basedOn")
        based_id = based.get(W_VAL) if based is not None else None
        raw[sid] = (fonts, based_id)

    def resolve(sid, seen=None):
        seen = seen or set()
        if sid in seen or sid not in raw:
            return set()
        seen.add(sid)
        fonts, based = raw[sid]
        result = set(fonts)
        if based:
            result.update(resolve(based, seen))
        return result

    for sid in raw:
        style_fonts[sid] = resolve(sid)
    return style_fonts, default_fonts


def _font_names_from_rpr(rpr):
    if rpr is None:
        return set()
    names = set()
    rfonts = rpr.find(W_RFONTS)
    if rfonts is not None:
        names.update(v for v in rfonts.attrib.values() if v)
    return names


def _is_legacy_font_name(font_names, legacy_names):
    legacy = {" ".join(str(x).lower().split()) for x in legacy_names}
    for name in font_names:
        normalized = " ".join(str(name).lower().split())
        if normalized in legacy:
            return True
        if any(normalized.startswith(x + " ") or normalized.startswith(x + "-") for x in legacy):
            return True
    return False


def _paragraph_style_font_names(run, style_fonts):
    paragraph = run
    while paragraph is not None and paragraph.tag != W_P:
        paragraph = getattr(paragraph, "_parent", None)
    # ElementTree has no parent pointers. The caller therefore supplies
    # paragraph-style information separately where possible. Returning empty
    # here is safe because direct run fonts and document defaults are checked.
    return set()


def _run_uses_legacy_font(run, legacy_names, style_fonts, default_fonts):
    rpr = run.find(W_RPR)
    direct = _font_names_from_rpr(rpr)
    if direct:
        return _is_legacy_font_name(direct, legacy_names)
    if rpr is not None:
        rstyle = rpr.find(W_RSTYLE)
        if rstyle is not None:
            sid = rstyle.get(W_VAL)
            if sid and _is_legacy_font_name(style_fonts.get(sid, set()), legacy_names):
                return True
    if default_fonts:
        return _is_legacy_font_name(default_fonts, legacy_names)
    # The selected source family is authoritative when the run has no font metadata.
    return True


def _set_docx_run_font(run, output_font):
    rpr = run.find(W_RPR)
    if rpr is None:
        rpr = ET.Element(W_RPR)
        run.insert(0, rpr)
    rfonts = rpr.find(W_RFONTS)
    if rfonts is None:
        rfonts = ET.Element(W_RFONTS)
        rpr.insert(0, rfonts)
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        rfonts.set(f"{{{W_NS}}}{key}", output_font)


def _set_drawing_run_font(run, output_font):
    rpr = run.find(A_RPR)
    if rpr is None:
        rpr = ET.Element(A_RPR)
        run.insert(0, rpr)
    rpr.set("latin", output_font)
    rpr.set("ea", output_font)
    rpr.set("cs", output_font)


def _convert_docx_xml(data, convert_text, legacy_names, output_font, style_fonts, default_fonts):
    root = _parse(data)
    converted_nodes = 0
    for run in root.iter(W_R):
        if not _run_uses_legacy_font(run, legacy_names, style_fonts, default_fonts):
            continue
        changed = False
        for node in list(run.iter(W_T)) + list(run.iter(W_DEL_T)):
            original = node.text or ""
            if not original:
                continue
            converted = convert_text(unescape(original))
            if converted != original:
                node.text = converted
                changed = True
                converted_nodes += 1
        if changed:
            _set_docx_run_font(run, output_font)

    for run in root.iter(A_R):
        changed = False
        for node in run.iter(A_T):
            original = node.text or ""
            if not original:
                continue
            converted = convert_text(unescape(original))
            if converted != original:
                node.text = converted
                changed = True
                converted_nodes += 1
        if changed:
            _set_drawing_run_font(run, output_font)
    return _serialize(root), converted_nodes


def _convert_odt_xml(data, convert_text):
    root = _parse(data)
    changed = 0
    for node in root.iter():
        if node.text:
            original = node.text
            converted = convert_text(unescape(original))
            if converted != original:
                node.text = converted
                changed += 1
        if node.tail:
            original = node.tail
            converted = convert_text(unescape(original))
            if converted != original:
                node.tail = converted
                changed += 1
    return _serialize(root), changed


def convert_document(input_path, output_path, convert_text, overwrite=False,
                     legacy_font_names=None, output_font="Lohit Gujarati"):
    source = Path(input_path)
    destination = Path(output_path)
    extension = source.suffix.lower()
    if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ValueError(f"Unsupported document format: {extension}")
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Input file does not exist: {source}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {destination}")

    legacy_font_names = legacy_font_names or set()
    with ZipFile(source, "r") as zin:
        names = zin.namelist()
        infos = {info.filename: info for info in zin.infolist()}
        entries = {}
        style_fonts, default_fonts = ({}, set())
        if extension == ".docx" and "word/styles.xml" in names:
            style_fonts, default_fonts = _read_style_font_map(zin.read("word/styles.xml"))
        parts = _docx_content_parts(names) if extension == ".docx" else [
            n for n in names if n.lower() in {"content.xml", "styles.xml"}
        ]
        converted_nodes = 0
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename in parts:
                if extension == ".docx":
                    data, count = _convert_docx_xml(
                        data, convert_text, legacy_font_names, output_font,
                        style_fonts, default_fonts,
                    )
                else:
                    data, count = _convert_odt_xml(data, convert_text)
                converted_nodes += count
            entries[info.filename] = data

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    try:
        with ZipFile(temp, "w") as zout:
            for name, data in entries.items():
                zout.writestr(infos[name], data)
        temp.replace(destination)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    return {
        "success": True,
        "input_path": str(source),
        "output_path": str(destination),
        "format": extension[1:].upper(),
        "xml_parts_checked": len(parts),
        "text_nodes_converted": converted_nodes,
        "output_font": output_font if extension == ".docx" else None,
    }
