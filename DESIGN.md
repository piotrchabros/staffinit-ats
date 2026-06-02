# StaffInit ATS — Design System (v1)

Small, intentional system for an internal-first recruiting tool. Calm app-UI
surface, strong typography, the **score is the hero**. No decoration for its own
sake.

## Typeface

**IBM Plex Sans** (self-hosted woff2 in `ats/static/fonts/` — never hotlink Google
Fonts; that's a GDPR liability for an EU app). Weights: 400 body, 500 emphasis,
600 headings/labels, 700 the score. Fallback stack only if the font fails to load.

## Color tokens (CSS variables in `base.html`)

| Token | Value | Use |
|---|---|---|
| `--ink` | `#14161a` | Primary text, primary buttons |
| `--muted` | `#5b6470` | Secondary text, labels |
| `--line` | `#e6e8eb` | Borders, dividers |
| `--bg` | `#f7f8fa` | Page background |
| `--surface` | `#ffffff` | Cards, table |
| `--accent` | `#0d9488` (teal) | Links, focus rings, brand mark |

**Score bands** (the only place green/amber/red appear — they mean *quality*, so
nothing else competes for those colors):

| Band | Threshold (0-100) | Text | Tint |
|---|---|---|---|
| good | ≥ 70 | `#15803d` | `#e7f6ec` |
| mid | 40–69 | `#b45309` | `#fff6e5` |
| bad | < 40 | `#b91c1c` | `#fdecec` |

Thresholds also live in `ats/templatetags/score_extras.py` (`score_band`).

## Principles applied

- **Score as hero.** The overall score renders large, weight 700, in its band
  color, with a thin band bar. A recruiter ranks candidates in one scan.
- **Status is neutral; score carries color.** Scored rows show the colored score
  (no redundant green chip). Pending = amber chip, Failed = red chip + retry.
- **Criteria are scannable inline** (`5·4·5` mini-scores) with rationale behind a
  disclosure — evidence is one glance away, detail one click away.
- **One job per surface, subtraction default.** No gradients, no card grids, no
  icons-in-circles. Cards only when the card is the interaction.

## Responsive

Tables become **stacked cards** below 720px (not horizontal overflow): `thead`
hides, each row is a card, and `td::before` shows the column label from
`data-label`. The score hero and inline criteria stay legible. The add-candidate
grid collapses to one column. Control rows wrap.

## Accessibility

- Skip-to-content link → `#main`; `banner` + `main` landmarks.
- Every form control has an associated `<label>` (visible or `visually-hidden`);
  the paste box and rubric `<select>` are labeled; the scorecard table has a
  `<caption>`; headers use `scope="col"`.
- Visible focus rings (`:focus-visible`) on links, buttons, summaries, selects.
- `--accent` is teal-700 (~4.5:1 on white) so link/label text passes WCAG AA.
  Score bars are `aria-hidden` (decorative); the number carries the value.
- Touch targets ≥44px on coarse pointers.
- Rubric filter auto-submits with JS but has an "Apply" button for no-JS/keyboard.

## Known gaps (tracked)

- A brand mark / favicon beyond the wordmark.
- A full screen-reader pass on a real device (the above is structural, not audited live).
