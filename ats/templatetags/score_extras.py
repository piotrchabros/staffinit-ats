"""Template helpers for rendering scores."""

from django import template

register = template.Library()

# Overall is 0-100. Bands drive the color of the score hero so a recruiter can
# rank candidates at a glance. Thresholds live here (and in DESIGN.md), not
# scattered across templates.
GOOD = 70
MID = 40


@register.filter
def score_band(overall):
    """Return 'good' | 'mid' | 'bad' for a 0-100 overall score (or '' if None)."""
    if overall is None:
        return ""
    try:
        value = float(overall)
    except (TypeError, ValueError):
        return ""
    if value >= GOOD:
        return "good"
    if value >= MID:
        return "mid"
    return "bad"
