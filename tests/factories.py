"""Test data builders.

Resume documents are generated in memory rather than committed as binary
fixtures, so the suite has no opaque test assets and the generators double as
a check that the extractors handle real files.
"""

from __future__ import annotations

from io import BytesIO

import docx

from tests.conftest import TEST_RECORD_MARKER
from tests.pdf_builder import build_blank_pdf, build_pdf_from_text

# Worked example from CLAUDE.md section 3: Finance / Financial Analyst.
FINANCIAL_ANALYST_RESUME = f"""Jane {TEST_RECORD_MARKER} Doe
jane.doe@test.invalid | New York, NY

PROFESSIONAL SUMMARY
Detail-oriented finance professional with 6 years of experience in financial
planning and analysis, budgeting and corporate finance.

WORK EXPERIENCE
Senior Financial Analyst | Acme Corporation | Jan 2021 - Present
- Built financial models and forecasting tools supporting a $40M budget
- Led monthly variance analysis and reporting for three business units
- Developed FP&A dashboards in Power BI used by executive leadership

Financial Analyst | Globex Inc | Mar 2019 - Dec 2020
- Prepared budgeting packages and quarterly financial reporting
- Automated Excel reporting workflows, reducing close time by 30%

EDUCATION
Bachelor of Science in Finance, New York University, 2018

SKILLS
Financial modeling, forecasting, budgeting, variance analysis, FP&A,
Advanced Excel, Power BI, SQL, SAP

CERTIFICATIONS
CFA Level II Candidate
"""

BACKEND_ENGINEER_RESUME = f"""John {TEST_RECORD_MARKER} Smith

SUMMARY
Backend engineer building distributed services.

EXPERIENCE
Backend Engineer | Initech | Feb 2021 - Present
- Designed REST APIs serving 2M requests per day
- Migrated services to Kubernetes and Docker

Junior Software Engineer | Hooli | Jun 2019 - Jan 2021
- Developed Python microservices backed by PostgreSQL

EDUCATION
Bachelor of Science in Computer Science, State University, 2019

SKILLS
Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, REST API, Git
"""

# No recognisable role, section or skill - used for the low-signal path.
UNPARSEABLE_RESUME = """Hello there

I like long walks and interesting conversations about nothing in particular.
Please get in touch if that sounds appealing to you as well.
"""


def make_txt(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    return text.encode("utf-8")


def make_pdf(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    """Build a real PDF with selectable text."""
    return build_pdf_from_text(text)


def make_scanned_pdf() -> bytes:
    """A PDF with pages but no selectable text, like a scan."""
    return build_blank_pdf()


def make_docx(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    """Build a real DOCX, including a table to exercise table extraction."""
    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)

    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Tools"
    table.rows[0].cells[1].text = "Tableau"

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def upload_files(
    data: bytes, filename: str = "resume.pdf", content_type: str = "application/pdf"
) -> dict[str, tuple[str, bytes, str]]:
    """Build the multipart payload for httpx."""
    return {"file": (filename, data, content_type)}
