from fpdf import FPDF
from pathlib import Path

font_path = r'C:\Windows\Fonts\arial.ttf'
bold_path = r'C:\Windows\Fonts\arialbd.ttf'
italic_path = r'C:\Windows\Fonts\ariali.ttf'
mono_path = r'C:\Windows\Fonts\cour.ttf'

pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.set_margins(20, 15, 20)
pdf.add_font('Body', style='', fname=font_path)
pdf.add_font('Body', style='B', fname=bold_path)
pdf.add_font('Body', style='I', fname=italic_path)
pdf.add_font('Mono', style='', fname=mono_path)
pdf.add_page()

md = open('docs/multilogin-ids-guide-ru.md', encoding='utf-8').read()
in_code = False
for i, line in enumerate(md.splitlines()):
    s = line.rstrip()
    try:
        if s.startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            pdf.set_font('Mono', size=9)
            pdf.set_fill_color(240, 240, 240)
            pdf.multi_cell(0, 5, s, fill=True)
        elif s.startswith('# '):
            pdf.set_font('Body', 'B', 16)
            pdf.multi_cell(0, 9, s[2:])
        elif s == '':
            pdf.ln(3)
        else:
            pdf.set_font('Body', size=10)
            print(f'Line {i}: x={pdf.get_x():.1f} y={pdf.get_y():.1f} w={pdf.w - pdf.l_margin - pdf.r_margin:.1f}')
            pdf.multi_cell(0, 6, s)
    except Exception as e:
        print(f'FAIL Line {i}: {repr(s[:80]).encode("ascii","replace").decode()} -> {e}')
        break
print('OK')
