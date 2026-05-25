"""CSRankings data loader: fetch faculty + area info from CSRankings GitHub."""

import csv
import io
import requests

CSRANKINGS_CSV = "https://raw.githubusercontent.com/emeryberger/CSRankings/gh-pages/csrankings.csv"
AUTHOR_INFO_CSV = "https://raw.githubusercontent.com/emeryberger/CSRankings/gh-pages/generated-author-info.csv"

# AI-related area codes from CSRankings
AI_AREAS = {
    "aaai", "ijcai",                          # AI general
    "cvpr", "eccv", "iccv",                   # Vision
    "icml", "iclr", "kdd", "nips",            # ML / Data Mining
    "acl", "emnlp", "naacl",                  # NLP
    "sigir", "www",                           # IR / Web
    "icra", "iros", "rss",                    # Robotics
    "miccai",                                 # Medical Imaging
    "uai", "aistats", "colt",                 # AI theory / stats
    "interspeech",                            # Speech
    "mm",                                     # Multimedia
    "chi",                                    # HCI (many do AI+HCI)
    "sc", "hpca", "isca", "micro",            # Systems / AI infra
    "neurips",                                # NeurIPS alternate code
    "sigmod", "vldb", "icde",                 # Data (ML for DB / DB for ML)
    "ccs", "sp", "uss",                       # Security (AI for security)
}

_faculty_cache = None
_areas_cache = None


def fetch_faculty() -> list[dict]:
    """Fetch csrankings.csv → list of {name, affiliation, homepage, scholarid}."""
    global _faculty_cache
    if _faculty_cache is not None:
        return _faculty_cache

    resp = requests.get(CSRANKINGS_CSV, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    _faculty_cache = []
    for row in reader:
        _faculty_cache.append({
            "name": row.get("name", "").strip(),
            "affiliation": row.get("affiliation", "").strip(),
            "homepage": row.get("homepage", "").strip(),
            "scholarid": row.get("scholarid", "").strip(),
        })
    return _faculty_cache


def fetch_author_areas() -> dict[str, set[str]]:
    """Fetch generated-author-info.csv → {name: set of area codes}."""
    global _areas_cache
    if _areas_cache is not None:
        return _areas_cache

    resp = requests.get(AUTHOR_INFO_CSV, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    _areas_cache = {}
    for row in reader:
        name = row.get("name", "").strip()
        area = row.get("area", "").strip()
        if name and area:
            _areas_cache.setdefault(name, set()).add(area)
    return _areas_cache


def get_institutions() -> list[str]:
    """Return sorted list of all unique institutions."""
    faculty = fetch_faculty()
    return sorted(set(f["affiliation"] for f in faculty if f["affiliation"]))


def get_ai_faculty(institution: str) -> list[dict]:
    """Get AI-related faculty for a given institution.

    Returns list of {name, affiliation, homepage, scholarid, areas}
    where areas is a list of AI area codes the professor publishes in.
    """
    faculty = fetch_faculty()
    author_areas = fetch_author_areas()

    results = []
    seen = set()
    for f in faculty:
        if f["affiliation"].lower() != institution.lower():
            continue
        if f["name"] in seen:
            continue

        person_areas = author_areas.get(f["name"], set())
        ai_overlap = person_areas & AI_AREAS
        if not ai_overlap:
            continue

        seen.add(f["name"])
        results.append({
            **f,
            "areas": sorted(ai_overlap),
        })

    results.sort(key=lambda x: x["name"])
    return results


def get_all_faculty(institution: str) -> list[dict]:
    """Get ALL faculty at an institution (no area filter).

    Returns list of {name, affiliation, homepage, scholarid, areas}
    where areas is ALL area codes the professor publishes in.
    """
    faculty = fetch_faculty()
    author_areas = fetch_author_areas()

    results = []
    seen = set()
    for f in faculty:
        if f["affiliation"].lower() != institution.lower():
            continue
        if f["name"] in seen:
            continue

        seen.add(f["name"])
        person_areas = author_areas.get(f["name"], set())
        results.append({
            **f,
            "areas": sorted(person_areas),
        })

    results.sort(key=lambda x: x["name"])
    return results


AREA_LABELS = {
    "aaai": "AAAI", "ijcai": "IJCAI",
    "cvpr": "CVPR", "eccv": "ECCV", "iccv": "ICCV",
    "icml": "ICML", "iclr": "ICLR", "kdd": "KDD", "nips": "NeurIPS", "neurips": "NeurIPS",
    "acl": "ACL", "emnlp": "EMNLP", "naacl": "NAACL",
    "sigir": "SIGIR", "www": "WWW",
    "icra": "ICRA", "iros": "IROS", "rss": "RSS",
    "miccai": "MICCAI", "uai": "UAI", "aistats": "AISTATS", "colt": "COLT",
    "interspeech": "Interspeech", "mm": "ACM MM", "chi": "CHI",
    "sc": "SC", "hpca": "HPCA", "isca": "ISCA", "micro": "MICRO",
    "sigmod": "SIGMOD", "vldb": "VLDB", "icde": "ICDE",
    "ccs": "CCS", "sp": "S&P", "uss": "USENIX Sec",
}
