# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Crawls the Toronto District School Board (TDSB) website to extract geographical data (school pins + catchment boundary polygons) for Junior Public Schools and outputs a KML file ready to import into [mymaps.google.com](https://mymaps.google.com).

## Running

```bash
python3 scrape.py
```

Output: `tdsb_junior_schools.kml`

## How It Works

1. **School list** — fetched from a TDSB internal API:
   `AjaxResponse.aspx?ad=453dgdjhh218789&exec=getBounds&folder=Elementary`
   Returns XML with all TDSB school IDs, names, lat/lng, addresses, panel types, grade ranges.
   A `schools.xml` cache is used automatically if present (the API is intermittently unstable).

2. **Filter** — keeps schools with "Junior" in the name (~185 schools).

3. **Boundaries** — fetched concurrently (10 workers) from:
   `https://www.tdsb.on.ca/Find-your/School/By-Map/focusonschool/{school_id}`
   Each page embeds the polygon vertices as `bounds.extend(new google.maps.LatLng(lat, lng))` JS calls.
   Some alternative/special schools (~8) have no boundary data.

4. **KML output** — two folders: "Schools" (point placemarks) and "Catchment Boundaries" (polygons).

## Key URLs

- School list API: `https://www.tdsb.on.ca/DesktopModules/Tdsb.Webteam.Modules.SchoolSearchMap/AjaxResponse.aspx?ad=453dgdjhh218789&exec=getBounds&folder=Elementary`
- School boundary page: `https://www.tdsb.on.ca/Find-your/School/By-Map/focusonschool/{schno}`
- School profile: `https://www.tdsb.on.ca/FindYour/Schools.aspx?schno={schno}`

## Dependencies

Declared in `pyproject.toml`. Install with `pip install -e .` or `pip install httpx shapely`.
