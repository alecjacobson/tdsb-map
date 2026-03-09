#!/usr/bin/env python3
"""
Crawl the TDSB website for all Junior Public Schools and extract
geo data (pins + catchment boundaries) ready to import into mymaps.google.com.

Output: tdsb_junior_schools.kml
"""

import math
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from shapely.geometry import Polygon as ShapelyPolygon

SCHOOLS_API = (
    "https://www.tdsb.on.ca/DesktopModules/Tdsb.Webteam.Modules.SchoolSearchMap/"
    "AjaxResponse.aspx?ad=453dgdjhh218789&exec=getBounds&folder=Elementary"
)
FOCUSONSCHOOL_URL = "https://www.tdsb.on.ca/Find-your/School/By-Map/focusonschool/{id}"

HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_WORKERS = 10

# KML colors in AABBGGRR format (alpha, blue, green, red).
# 5 visually distinct, colorblind-friendly hues at ~35% opacity fill.
PALETTE = [
    # (fill,       line)
    ("59d44000", "ffd44000"),  # blue
    ("5900a550", "ff00a550"),  # green
    ("590000cc", "ff0000cc"),  # red
    ("59007fff", "ff007fff"),  # orange
    ("59cc4488", "ffcc4488"),  # purple
]


def _is_junior_school(name, grade_range, panel):
    """Return True for schools that serve Junior/Elementary grades.

    Includes any school with an Elementary panel whose grade range starts at
    JK or SK (i.e. genuine elementary schools, not intermediate-only).
    """
    return "Elementary" in panel and bool(re.match(r"(JK|SK|Grade UG)\s*-", grade_range))


def fetch_school_list(local_xml=None):
    """Fetch all school metadata from the TDSB API (or a local XML file)."""
    if local_xml:
        with open(local_xml, "rb") as f:
            data = f.read()
        root = ET.fromstring(data)
    else:
        resp = httpx.get(SCHOOLS_API, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    schools = []
    for school in root.findall("School"):
        name = school.findtext("name", "")
        grade_range = school.findtext("gradeRange", "")
        panel = school.findtext("RawPanelData", "")
        if not _is_junior_school(name, grade_range, panel):
            continue
        schools.append({
            "id": school.findtext("id"),
            "name": name,
            "lat": float(school.findtext("lat") or "nan"),
            "lng": float(school.findtext("lng") or "nan"),
            "address": school.findtext("address", ""),
            "city": school.findtext("city", ""),
            "postal_code": school.findtext("postalCode", ""),
            "phone": school.findtext("phone", ""),
            "grade_range": school.findtext("gradeRange", ""),
        })
    return schools


def fetch_boundary(school, client):
    """Fetch boundary polygon vertices for a school from its focusonschool page."""
    url = FOCUSONSCHOOL_URL.format(id=school["id"])
    try:
        resp = client.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        print(f"  WARNING: failed to fetch {url}: {e}", file=sys.stderr)
        return None

    raw = re.findall(
        r"bounds\.extend\(new google\.maps\.LatLng\(([^,]+),([^)]+)\)\)", content
    )
    if not raw:
        print(f"  WARNING: no boundary found for {school['name']}", file=sys.stderr)
        return None

    # Split the flat point list into rings: each ring closes back to its first point.
    rings, current = [], []
    for lat_s, lng_s in raw:
        pt = (float(lat_s), float(lng_s))
        current.append(pt)
        if len(current) > 1 and pt == current[0]:
            rings.append(current)
            current = []
    if current:  # unclosed trailing ring
        rings.append(current)
    return rings


def build_adjacency(schools_with_boundaries):
    """Return adjacency sets using shapely polygon intersection.

    Two regions are adjacent if they share an edge (not merely a corner point).
    """
    polys = []
    for _s, rings in schools_with_boundaries:
        if rings:
            try:
                from shapely.ops import unary_union
                parts = [ShapelyPolygon([(lng, lat) for lat, lng in ring])
                         for ring in rings if len(ring) >= 3]
                p = unary_union(parts)
                if not p.is_valid:
                    p = p.buffer(0)
                polys.append(p)
            except Exception:
                polys.append(None)
        else:
            polys.append(None)

    n = len(polys)
    adj = [set() for _ in range(n)]
    for i in range(n):
        if polys[i] is None:
            continue
        for j in range(i + 1, n):
            if polys[j] is None:
                continue
            try:
                shared = polys[i].intersection(polys[j])
                # Only count as adjacent if they share an edge, not just a point
                if not shared.is_empty and shared.geom_type not in ("Point", "MultiPoint"):
                    adj[i].add(j)
                    adj[j].add(i)
            except Exception:
                pass
    return adj


def greedy_color(n, adj):
    """Assign the lowest available palette index to each node.

    Processes nodes largest-degree-first for a better (fewer colors) result.
    """
    order = sorted(range(n), key=lambda i: -len(adj[i]))
    colors = [-1] * n
    for i in order:
        used = {colors[j] for j in adj[i] if colors[j] >= 0}
        c = 0
        while c in used:
            c += 1
        colors[i] = c
    return colors


def format_phone(raw):
    if len(raw) == 10 and raw.isdigit():
        return f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
    return raw


def escape_xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_kml(schools_with_boundaries, colors):
    num_colors = max(colors) + 1 if any(c >= 0 for c in colors) else 1
    print(f"  Graph coloring used {num_colors} colors", file=sys.stderr)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "  <name>TDSB Junior Public Schools</name>",
        "",
        "  <!-- Styles -->",
        '  <Style id="school_pin">',
        "    <IconStyle>",
        "      <color>ff0000ff</color>",
        "      <scale>1.0</scale>",
        "    </IconStyle>",
        "  </Style>",
    ]

    num_colors = max(colors) + 1 if colors else 1
    effective_palette = PALETTE * ((num_colors // len(PALETTE)) + 1)
    for i, (fill, line) in enumerate(effective_palette[:num_colors]):
        lines += [
            f'  <Style id="boundary_{i}">',
            "    <LineStyle>",
            f"      <color>{line}</color>",
            "      <width>2</width>",
            "    </LineStyle>",
            "    <PolyStyle>",
            f"      <color>{fill}</color>",
            "    </PolyStyle>",
            "  </Style>",
        ]

    lines += [
        "",
        "  <Folder>",
        "    <name>Schools</name>",
    ]

    for s, boundary in schools_with_boundaries:
        if math.isnan(s["lat"]) or math.isnan(s["lng"]):
            continue
        desc = escape_xml(
            f"{s['address']}  {s['postal_code']}\n"
            f"Phone: {format_phone(s['phone'])}\n"
            f"Grades: {s['grade_range'].rstrip(')')}"
        )
        lines += [
            "    <Placemark>",
            f"      <name>{escape_xml(s['name'])}</name>",
            f"      <description>{desc}</description>",
            '      <styleUrl>#school_pin</styleUrl>',
            "      <Point>",
            f"        <coordinates>{s['lng']},{s['lat']},0</coordinates>",
            "      </Point>",
            "    </Placemark>",
        ]

    lines += [
        "  </Folder>",
        "",
        "  <Folder>",
        "    <name>Catchment Boundaries</name>",
    ]

    def polygon_kml(ring, indent):
        if ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        coords = " ".join(f"{lng},{lat},0" for lat, lng in ring)
        i = " " * indent
        return [
            f"{i}<Polygon>",
            f"{i}  <outerBoundaryIs>",
            f"{i}    <LinearRing>",
            f"{i}      <coordinates>{coords}</coordinates>",
            f"{i}    </LinearRing>",
            f"{i}  </outerBoundaryIs>",
            f"{i}</Polygon>",
        ]

    for (s, rings), color in zip(schools_with_boundaries, colors):
        if not rings:
            continue
        style_id = f"boundary_{color}"
        lines += [
            "    <Placemark>",
            f"      <name>{escape_xml(s['name'])} - Boundary</name>",
            f"      <styleUrl>#{style_id}</styleUrl>",
        ]
        if len(rings) == 1:
            lines += polygon_kml(rings[0], indent=6)
        else:
            lines.append("      <MultiGeometry>")
            for ring in rings:
                lines += polygon_kml(ring, indent=8)
            lines.append("      </MultiGeometry>")
        lines.append("    </Placemark>")

    lines += [
        "  </Folder>",
        "</Document>",
        "</kml>",
    ]
    return "\n".join(lines)


def main():
    local_xml = "schools.xml" if __import__("os").path.exists("schools.xml") else None
    if local_xml:
        print(f"Using cached school data from {local_xml}", file=sys.stderr)
    else:
        print("Fetching school list from TDSB API...", file=sys.stderr)
    schools = fetch_school_list(local_xml)
    print(f"Found {len(schools)} Junior schools", file=sys.stderr)

    print("Fetching catchment boundaries...", file=sys.stderr)
    results = [None] * len(schools)

    with httpx.Client() as client:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_boundary, s, client): i
                for i, s in enumerate(schools)
            }
            done = 0
            for future in as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                done += 1
                if done % 10 == 0:
                    print(f"  {done}/{len(schools)} done", file=sys.stderr)

    schools_with_boundaries = list(zip(schools, results))
    missing = sum(1 for _, b in schools_with_boundaries if b is None)
    if missing:
        print(f"WARNING: {missing} schools have no boundary data", file=sys.stderr)

    print("Computing neighbor graph and coloring regions...", file=sys.stderr)
    adj = build_adjacency(schools_with_boundaries)
    colors = greedy_color(len(schools_with_boundaries), adj)

    print("Building KML...", file=sys.stderr)
    kml = build_kml(schools_with_boundaries, colors)

    out = "tdsb_junior_schools.kml"
    with open(out, "w", encoding="utf-8") as f:
        f.write(kml)

    print(f"Done. Written to {out}", file=sys.stderr)
    print(f"  {len(schools)} schools, {len(schools) - missing} with boundaries")


if __name__ == "__main__":
    main()
