"""Writes the eval documents that aren't plain text (.docx, .pdf, .xlsx) from the text below.

The generated files are committed, so running the evals doesn't need this. Rerun it after
editing the text here, then check evals/gold still matches:

    python -m evals.make_fixtures
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

CASES = Path(__file__).resolve().parent / "cases"

# Fixed timestamp inside the .docx zip, so regenerating unchanged text gives identical bytes.
_ZIP_TIME = (2023, 1, 1, 0, 0, 0)

GRIEVANCE_LETTER = """\
Daniel Okafor
14 Kingsweston Lane
Bristol BS11 0JE

3 May 2023

Ms Michelle Byrne
HR Business Partner
Northgate Distribution Ltd

Dear Ms Byrne,

FORMAL GRIEVANCE

I am writing to raise a formal grievance under the company's grievance procedure about the way I have been treated by my shift manager, Karen Holt.

On Friday 14 April 2023 I took forklift FLT 3 out of use at the start of my shift because the brakes were spongy and the truck was not stopping properly. This was the second time that week. I tagged it out and recorded the fault on the defect sheet. Karen Holt told me on the team Slack channel not to be "dramatic" and that she needed the truck back for the 9am load. Later that morning, in front of Jas Sandhu, she said to me "if you can't handle the pace, find another job".

On Monday 17 April 2023 Karen Holt took me off the Saturday overtime rota, which I have worked for three years. When I asked why, she said "operational reasons" and would not discuss it. This costs me about £180 a week. I believe I was taken off overtime because I refused to use an unsafe vehicle.

Because of the stress of this I was signed off sick by my GP from 24 April 2023 to 5 May 2023.

I would like:
1. to be put back on the Saturday overtime rota and paid for the shifts I have lost;
2. FLT 3 to be properly inspected before anyone uses it again; and
3. an apology from Karen Holt.

I would like to be accompanied at any meeting by my colleague Jaswinder Sandhu.

Yours sincerely,

Daniel Okafor
Team Leader, Warehouse B (nights/early)
"""

QUOTATION = [
    ("HOLLIS & SONS BUILDING LTD", "title"),
    ("Kitchens - Extensions - Loft conversions | Unit 4 Station Yard, Keynsham BS31 2BH | Co. no. 08812345 | VAT GB 291 4400 12", "small"),
    ("QUOTATION  Q-2231", "heading"),
    ("Date: 28/04/2023        Customer: Mrs P. Nair, 9 Elm Grove, Saltford BS31 3EE", "body"),
    ("Scope of works: strip out existing kitchen; first-fix plumbing and electrics; supply and fit "
     "customer-chosen units (Howdens Clerkenwell, ref. HK-7781); 30 mm quartz worktops with undermount "
     "sink; tiling; extractor hood ducted to outside; making good and decoration.", "body"),
    ("Price: £15,500.00 + VAT £3,100.00 = TOTAL £18,600.00", "body"),
    ("Programme: start Monday 5 June 2023, approx. 6 weeks on site.", "body"),
    ("Payment terms: 30% deposit (£5,580.00) on acceptance; stage payment of £6,000.00 on completion "
     "of first fix and worktop install; balance (£7,020.00) on completion. Invoices payable within 7 days.", "body"),
    ("Guarantee: all workmanship guaranteed for 12 months from completion. Defects reported within "
     "this period will be put right at no cost.", "body"),
    ("Terms and conditions: see overleaf. This quotation is valid for 30 days.", "small"),
    ("ACCEPTANCE: I accept this quotation and the terms and conditions.", "heading"),
    ("Signed: Priya Nair        Date: 02/05/2023", "body"),
    ("For Hollis & Sons: D. Hollis, Director", "body"),
]

PAYMENTS = [
    ("Date", "Payee", "Description", "Invoice", "Amount (£)", "Method", "Status"),
    (date(2023, 5, 3), "Hollis & Sons Building Ltd", "Kitchen deposit 30%", "INV-1904", 5580.00, "Bank transfer", "Paid"),
    (date(2023, 5, 20), "Howdens (via Hollis)", "Appliance upgrade - induction hob", "n/a", 0, "", "Cancelled - kept old hob"),
    (date(2023, 6, 26), "Hollis & Sons Building Ltd", "Stage payment - first fix + worktops", "INV-1951", 6000.00, "Bank transfer", "Paid"),
    (date(2023, 7, 31), "Hollis & Sons Building Ltd", "Final invoice - balance", "INV-1987", 7020.00, "", "NOT PAID - withheld pending defects (see letter 21/08)"),
    (None, None, None, "Total paid", "=SUM(E2:E4)", None, None),
]


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries.items():
            zf.writestr(zipfile.ZipInfo(name, _ZIP_TIME), text)
    return buf.getvalue()


def make_docx(text: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in text.split("\n")
    )
    return _zip({
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{w}"><w:body>'
            f"{paragraphs}</w:body></w:document>"
        ),
    })


def make_pdf(blocks: list[tuple[str, str]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    style = {"title": styles["Title"], "heading": styles["Heading2"], "body": styles["BodyText"], "small": styles["Italic"]}
    buf = io.BytesIO()
    # invariant=True drops the creation date and random ID, so the bytes are reproducible.
    doc = SimpleDocTemplate(buf, pagesize=A4, invariant=True, title="Quotation Q-2231")
    story = []
    for text, kind in blocks:
        story += [Paragraph(escape(text), style[kind]), Spacer(1, 6)]
    doc.build(story)
    return buf.getvalue()


def make_xlsx(rows: list[tuple]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Kitchen 2023"
    for row in rows:
        ws.append(list(row))
    for cell in ws["A"][1:]:
        cell.number_format = "DD/MM/YYYY"
    wb.properties.created = wb.properties.modified = datetime(2023, 8, 1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    files = {
        CASES / "workplace-grievance" / "Grievance letter - D Okafor.docx": make_docx(GRIEVANCE_LETTER),
        CASES / "kitchen-renovation" / "Hollis quotation Q-2231 signed.pdf": make_pdf(QUOTATION),
        CASES / "kitchen-renovation" / "kitchen payments.xlsx": make_xlsx(PAYMENTS),
    }
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(CASES.parent)}")


if __name__ == "__main__":
    main()
