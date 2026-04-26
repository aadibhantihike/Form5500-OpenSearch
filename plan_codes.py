"""
Plain-English translation layer for Form 5500 plan-type codes.

The DOL stores plan-level benefit characteristics as concatenated 2-character
codes (e.g. '4A4B4D' = Health + Life + Dental). This module turns those into
human labels, derives sales-relevant facts (funding type, coverage gaps, plan
profile summary), and exposes a single glossary that the frontend uses to
render tooltips. No DB or external deps.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# 2-character codes only ever start with 1-4. Tight regex avoids false matches
# in any code paths that accidentally pass non-code strings.
_CODE_RE = re.compile(r"[1-4][A-Z]")


# ---------- Code dictionaries ----------

PENSION_LABELS: dict[str, str] = {
    "1A": "Profit-sharing",
    "1B": "Stock bonus",
    "1C": "Money purchase",
    "1D": "Target benefit",
    "1E": "ESOP",
    "1F": "Other defined contribution",
    "1G": "Defined benefit",
    "1H": "Cash balance",
    "1I": "DB floor-offset",
    "1J": "DC with auto-enrollment",
    "2A": "Age/service-weighted DC",
    "2C": "Money purchase",
    "2E": "Profit-sharing",
    "2F": "ERISA 404(c)",
    "2G": "Participant-directed",
    "2H": "Self-employed plan",
    "2I": "Hybrid plan",
    "2J": "401(k)",
    "2K": "SEP",
    "2L": "SIMPLE",
    "2M": "Restoration period",
    "2P": "Pre-approved pension",
    "2R": "Spin-off / merger",
    "2S": "ERISA 4063 plan",
    "2T": "Cash or deferred 401(k)",
    "3B": "PBGC-covered",
    "3C": "Not intended to qualify",
    "3D": "Pre-approved",
    "3F": "Auto-enrollment",
    "3H": "Frozen",
    "3I": "Soft frozen",
    "3J": "Hard frozen",
}

WELFARE_LABELS: dict[str, str] = {
    "4A": "Health",
    "4B": "Life",
    "4C": "Supplemental unemployment",
    "4D": "Dental",
    "4E": "Vision",
    "4F": "Short-term disability",
    "4G": "Long-term disability",
    "4H": "Severance",
    "4I": "Apprenticeship/training",
    "4J": "Scholarship",
    "4K": "Other death benefits",
    "4L": "Cafeteria (Section 125)",
    "4M": "401(h) account",
    "4N": "Multi-employer 401(h)",
    "4P": "Taft-Hartley",
    "4Q": "Other welfare",
    "4R": "Unfunded / fully insured",
    "4S": "HRA",
    "4T": "HSA",
    "4U": "Stop-loss insurance",
}

# Long-form descriptions used as tooltips. Sales-oriented framing where
# applicable; objective definition where not.
TOOLTIPS: dict[str, str] = {
    # Welfare — the codes a benefits rep actually cares about
    "4A": "Health (medical) coverage. The core target for a benefits broker pitch.",
    "4B": "Group life insurance. Often bundled with health; a common cross-sell hook.",
    "4D": "Dental coverage. Frequent cross-sell gap when missing.",
    "4E": "Vision coverage. Frequent cross-sell gap when missing.",
    "4F": "Short-term disability. Cross-sell gap when missing.",
    "4G": "Long-term disability. Cross-sell gap when missing.",
    "4H": "Severance pay benefit. Niche.",
    "4L": "Section 125 cafeteria plan — pre-tax election structure. Indicates plan-design sophistication.",
    "4Q": "Catch-all 'other welfare' bucket. Investigate manually.",
    "4R": "Funding indicator: unfunded / fully insured / combination. Used together with Schedule A presence to infer self-funded vs. insured.",
    "4S": "Health Reimbursement Arrangement. Modern reimbursement design.",
    "4T": "Health Savings Account. Almost always paired with a high-deductible plan; opens HSA-admin and decision-support sales motions.",
    "4U": "Stop-loss insurance. Strong signal of self-funded health plan.",

    # Pension — broader strokes (less critical for benefits brokers)
    "1G": "Defined benefit pension. Old-school, PBGC-covered if private. Often signals a unionized or mature workforce.",
    "1H": "Cash balance plan — hybrid DB/DC.",
    "2J": "401(k) feature on a defined-contribution plan. The most common modern retirement plan.",
    "2K": "Simplified Employee Pension. Small-employer signal.",
    "2L": "SIMPLE pension plan. Small-employer signal.",
    "3B": "Plan covered by Pension Benefit Guaranty Corporation. Real DB obligations.",
    "3F": "Plan provides automatic enrollment.",
    "3H": "Plan is frozen — no new participants, possibly no new accruals.",
}


# ---------- Helpers ----------

def parse_codes(s: Optional[str]) -> list[str]:
    """Pull 2-character codes out of a concatenated string like '4A4B4D'."""
    if not s:
        return []
    return _CODE_RE.findall(s.upper())


def humanize(code_str: Optional[str], label_map: dict[str, str]) -> list[str]:
    """Translate a code string into ordered human labels, deduped, no unknowns."""
    seen: set[str] = set()
    out: list[str] = []
    for c in parse_codes(code_str):
        label = label_map.get(c)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def humanize_pension(code_str: Optional[str]) -> list[str]:
    return humanize(code_str, PENSION_LABELS)


def humanize_welfare(code_str: Optional[str]) -> list[str]:
    return humanize(code_str, WELFARE_LABELS)


def _has_indicator(rows: Iterable[dict], col: str) -> bool:
    return any(r.get(col) == "1" for r in rows)


def funding_type(filing: dict, sch_a_rows: Iterable[dict]) -> str:
    """
    Heuristic funding classification. Schedule A files only when an insurance
    contract exists, so:
      - welfare_code includes 4A + Schedule A row with stop-loss flag → Self-funded
      - welfare_code includes 4A + Schedule A row covering health      → Fully insured
      - welfare_code includes 4A but no Schedule A health contract     → Likely self-funded (ASO)
      - no 4A in welfare_code                                          → n/a
    """
    welfare_codes = parse_codes(filing.get("type_welfare_bnft_code"))
    if "4A" not in welfare_codes:
        return "n/a"
    rows = list(sch_a_rows)
    if _has_indicator(rows, "bnft_stop_loss_ind"):
        return "Self-funded"
    if _has_indicator(rows, "bnft_health_ind"):
        return "Fully insured"
    return "Likely self-funded"


def coverage_gaps(filing: dict, sch_a_rows: Iterable[dict]) -> list[str]:
    """
    Return list of common ancillary benefits the sponsor doesn't cover, given
    they have a health plan. If no health plan is on file, returns []. Only
    flags benefits that brokers commonly sell — niche codes like Severance
    are not treated as gaps.
    """
    welfare_codes = set(parse_codes(filing.get("type_welfare_bnft_code")))
    if "4A" not in welfare_codes:
        return []
    rows = list(sch_a_rows)
    gaps: list[str] = []
    checks = [
        ("4D", "bnft_dental_ind", "Dental"),
        ("4E", "bnft_vision_ind", "Vision"),
        ("4B", "bnft_life_ind", "Life"),
        ("4G", "bnft_disability_ind", "Disability"),
    ]
    for code, ind_col, label in checks:
        if code not in welfare_codes and not _has_indicator(rows, ind_col):
            gaps.append(label)
    return gaps


def plan_profile(filing: dict, sch_a_rows: list[dict]) -> dict:
    """
    Compose the per-filing plan profile object the UI renders. Everything is
    derived; no DB calls. Safe to call inside a row loop.
    """
    welfare_types = humanize_welfare(filing.get("type_welfare_bnft_code"))
    pension_types = humanize_pension(filing.get("type_pension_bnft_code"))
    funding = funding_type(filing, sch_a_rows)
    gaps = coverage_gaps(filing, sch_a_rows)
    carriers = sorted({
        r.get("carrier_name") for r in sch_a_rows
        if r.get("carrier_name")
    })

    # Templated one-liner. Keep deliberately terse — the UI shows the chips
    # and pills alongside this; the summary is for skim-reading.
    bits: list[str] = []
    if welfare_types:
        if funding != "n/a":
            bits.append(f"{funding} medical")
        bits.append("offers " + ", ".join(welfare_types))
    if pension_types:
        bits.append("retirement: " + ", ".join(pension_types))
    if gaps:
        bits.append("gaps: " + ", ".join(gaps))
    summary = "; ".join(bits) if bits else "No plan-type codes filed."

    return {
        "welfare_types": welfare_types,
        "pension_types": pension_types,
        "funding_type": funding,
        "coverage_gaps": gaps,
        "carriers": carriers,
        "summary": summary,
    }


def glossary() -> dict:
    """Single payload the frontend caches for tooltip rendering."""
    return {
        "pension": {c: {"label": PENSION_LABELS[c], "tooltip": TOOLTIPS.get(c, "")}
                    for c in PENSION_LABELS},
        "welfare": {c: {"label": WELFARE_LABELS[c], "tooltip": TOOLTIPS.get(c, "")}
                    for c in WELFARE_LABELS},
        "funding_types": {
            "Self-funded": "Sponsor pays claims directly; carries stop-loss insurance to cap catastrophic risk. TPA / PBM / stop-loss sales motion.",
            "Fully insured": "Sponsor pays a premium to a carrier who assumes the claims risk. Traditional broker-of-record sales motion.",
            "Likely self-funded": "Health plan filed but no Schedule A insurance contract — typical of administrative-services-only arrangements.",
            "n/a": "No medical (4A) on this filing.",
        },
    }
