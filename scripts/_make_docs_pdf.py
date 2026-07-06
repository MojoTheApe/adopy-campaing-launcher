"""Convert Multilogin guide markdown files to PDF using fpdf2 with Unicode fonts."""
from pathlib import Path
from fpdf import FPDF
from fpdf.enums import XPos, YPos

DOCS = Path(__file__).resolve().parents[1] / "docs"

FILES = [
    ("multilogin-ids-guide-ru.md", "multilogin-ids-guide-ru.pdf"),
    ("multilogin-ids-guide-en.md", "multilogin-ids-guide-en.pdf"),
]

# Windows system fonts that support Cyrillic
_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path(r"C:\Windows\Fonts\tahoma.ttf"),
    Path(r"C:\Windows\Fonts\verdana.ttf"),
]
_MONO_CANDIDATES = [
    Path(r"C:\Windows\Fonts\cour.ttf"),    # Courier New
    Path(r"C:\Windows\Fonts\consola.ttf"), # Consolas
    Path(r"C:\Windows\Fonts\lucon.ttf"),   # Lucida Console
]

def _find_font(candidates):
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def _render(md_path: Path, pdf_path: Path) -> None:
    text = md_path.read_text(encoding="utf-8")

    font_path = _find_font(_FONT_CANDIDATES)
    mono_path = _find_font(_MONO_CANDIDATES)
    if not font_path:
        raise RuntimeError("No suitable Unicode font found in C:\\Windows\\Fonts")

    # Try to find bold/italic variants for Arial
    font_dir = Path(r"C:\Windows\Fonts")
    bold_path = str(font_dir / "arialbd.ttf") if (font_dir / "arialbd.ttf").is_file() else font_path
    italic_path = str(font_dir / "ariali.ttf") if (font_dir / "ariali.ttf").is_file() else font_path

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(20, 15, 20)

    # Register Unicode fonts
    pdf.add_font("Body", style="", fname=font_path)
    pdf.add_font("Body", style="B", fname=bold_path)
    pdf.add_font("Body", style="I", fname=italic_path)
    if mono_path:
        pdf.add_font("Mono", style="", fname=mono_path)
    else:
        pdf.add_font("Mono", style="", fname=font_path)

    pdf.add_page()

    in_code = False
    for line in text.splitlines():
        stripped = line.rstrip()

        if stripped.startswith("```"):
            in_code = not in_code
            if in_code:
                pdf.ln(1)
            else:
                pdf.ln(2)
            continue

        NX, NY = XPos.LMARGIN, YPos.NEXT

        if in_code:
            pdf.set_font("Mono", size=9)
            pdf.set_text_color(30, 30, 30)
            pdf.set_fill_color(240, 240, 240)
            pdf.multi_cell(0, 5, stripped, fill=True, new_x=NX, new_y=NY)
            continue

        if stripped.startswith("# "):
            pdf.set_font("Body", style="B", size=16)
            pdf.set_text_color(30, 80, 180)
            pdf.multi_cell(0, 9, stripped[2:], new_x=NX, new_y=NY)
            pdf.ln(2)
        elif stripped.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Body", style="B", size=13)
            pdf.set_text_color(30, 80, 180)
            pdf.multi_cell(0, 8, stripped[3:], new_x=NX, new_y=NY)
            pdf.ln(1)
        elif stripped.startswith("### "):
            pdf.set_font("Body", style="B", size=11)
            pdf.set_text_color(50, 50, 50)
            pdf.multi_cell(0, 7, stripped[4:], new_x=NX, new_y=NY)
        elif stripped.startswith("---"):
            pdf.set_draw_color(180, 180, 180)
            pdf.line(20, pdf.get_y(), 190, pdf.get_y())
            pdf.ln(4)
        elif stripped.startswith("|"):
            pdf.set_font("Mono", size=8)
            pdf.set_text_color(60, 60, 60)
            pdf.multi_cell(0, 5, stripped, new_x=NX, new_y=NY)
        elif stripped.startswith("> "):
            pdf.set_font("Body", style="I", size=9)
            pdf.set_text_color(100, 100, 100)
            pdf.multi_cell(0, 5, "  " + stripped[2:], new_x=NX, new_y=NY)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            pdf.set_font("Body", size=10)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(0, 6, "  \u2022 " + stripped[2:], new_x=NX, new_y=NY)
        elif stripped == "":
            pdf.ln(3)
        else:
            pdf.set_font("Body", size=10)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(0, 6, stripped, new_x=NX, new_y=NY)

    pdf.output(str(pdf_path))
    print(f"  Saved: {pdf_path}")


if __name__ == "__main__":
    for md_name, pdf_name in FILES:
        md_path = DOCS / md_name
        pdf_path = DOCS / pdf_name
        if not md_path.is_file():
            print(f"  Skipped (not found): {md_name}")
            continue
        _render(md_path, pdf_path)
    print("Done.")
