"""Golden-set fixtures for the scoring eval.

A small, fixed set of JDs + CVs with EXPECTED score bands and ordering. The point
is regression protection: when you change the scoring prompt or rubric, this set
tells you whether the scores still behave sensibly and consistently — your
"consistency is king" seatbelt.

Keep this set anonymized and synthetic. Grow it from the rubric assignment
(3 real roles) but scrub PII before committing.
"""

# The rubric under test. Mirrors the shape stored in the Rubric model.
RUBRIC = [
    {"name": "Python", "scale": 5, "weight": 0.40,
     "description": "Depth and recency of professional Python."},
    {"name": "Backend/AWS", "scale": 5, "weight": 0.35,
     "description": "Backend systems design plus hands-on AWS cloud experience."},
    {"name": "Seniority", "scale": 5, "weight": 0.25,
     "description": "Years of experience and scope/ownership/leadership."},
]

JD_BACKEND = """\
Senior Backend Engineer (Python / AWS)
We need a senior engineer to own our Python (Django) services on AWS. You will
design APIs, run services in production on ECS, and mentor mid-level engineers.
Requirements: 5+ years professional Python, strong AWS (ECS/RDS/S3), API design,
production ownership. Nice to have: team leadership.
"""

CV_STRONG = """\
Anna N. — Senior Software Engineer (8 years)
- 8 years professional Python; Django and FastAPI in production.
- Designed and ran microservices on AWS ECS with RDS Postgres and S3.
- Led a team of 4; owned on-call and incident response.
- Built public REST APIs serving 2M req/day.
"""

CV_MID = """\
Piotr W. — Software Engineer (3 years)
- 3 years Python (Flask), some Django.
- Deployed a couple of services to AWS (EC2, S3); no ECS.
- Individual contributor; no leadership yet.
"""

CV_WEAK = """\
Marek L. — Junior Developer (1 year)
- 1 year experience, mostly PHP and WordPress.
- A little Python scripting on the side; no professional Python.
- No cloud experience.
"""

# Each case: a JD + CV with an expected overall band (0-100, inclusive).
# Bands are calibrated to OBSERVED model behavior (not guessed): against a
# senior JD the model rightly scores a 3yr/no-ECS/no-lead candidate low, so
# mid's floor is wide. The ceilings still catch a regression that inflates a
# poor fit. Re-tune these from real roles (the rubric assignment).
CASES = [
    {"label": "strong-backend", "jd": JD_BACKEND, "cv": CV_STRONG, "min": 70, "max": 100},
    {"label": "mid-backend", "jd": JD_BACKEND, "cv": CV_MID, "min": 15, "max": 65},
    {"label": "weak-backend", "jd": JD_BACKEND, "cv": CV_WEAK, "min": 0, "max": 40},
]

# Ordering invariants: (higher_label, lower_label) — higher must outscore lower.
ORDERING = [
    ("strong-backend", "mid-backend"),
    ("mid-backend", "weak-backend"),
]

# Re-score stability: score this case twice; overalls must be within tolerance.
STABILITY_LABEL = "strong-backend"
STABILITY_TOLERANCE = 15.0  # points on the 0-100 scale
