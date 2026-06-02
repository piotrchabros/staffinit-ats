"""Turn an uploaded file (or pasted text) into a CV row.

ingest_cv_file always creates a CV: if the file parsed, parsed_text is filled; if
not, the CV is stored with the raw file but empty text (CV.needs_manual_text is
True) so the UI can prompt a paste. ingest_pasted_cv is the manual fallback.
"""

from __future__ import annotations

from ats.models import CV, Candidate

from .parse import ParseResult, extract_text

MANUAL_PARSER = "manual-paste"


def ingest_cv_file(candidate: Candidate, django_file) -> tuple[CV, ParseResult]:
    """Read an uploaded file, extract text, and create a CV.

    Returns (cv, parse_result). When parse_result.ok is False, the CV is created
    with the file attached but empty parsed_text (needs_manual_text == True).
    """
    data = django_file.read()
    result = extract_text(getattr(django_file, "name", ""), data)

    # Rewind so Django can re-read the file when saving it to storage.
    try:
        django_file.seek(0)
    except (AttributeError, OSError):
        pass

    cv = CV.objects.create(
        candidate=candidate,
        raw_file=django_file,
        parsed_text=result.text if result.ok else "",
        parser_version=result.parser,
    )
    return cv, result


def ingest_pasted_cv(candidate: Candidate, text: str) -> CV:
    """Manual-paste fallback: store text directly, no file."""
    return CV.objects.create(
        candidate=candidate,
        parsed_text=text.strip(),
        parser_version=MANUAL_PARSER,
    )
