# Branding

Sources for the Plugin Store icon (`indigo-matter.indigoPlugin/Contents/Resources/icon.png`).

## What the icon is

An Indigo-purple house — the platform — framing the **Matter** symbol: Matter, inside Indigo.

## Files

| file | what it is |
|---|---|
| `icon-house.svg` | the house frame, hand-drawn for this plugin |
| `matter-logo-upstream.svg` | the official Matter lockup, fetched verbatim (see below) |
| `build-icon.py` | composites the two and writes `Contents/Resources/icon.png` |

Rebuild after changing either source:

```sh
python3 docs/branding/build-icon.py
```

Needs Chrome (headless) for rendering and Pillow for compositing. The script is
deterministic — an unchanged input reproduces an identical PNG.

The upstream file is a single path containing *both* the wordmark and the symbol,
so the symbol cannot be selected by path index. `build-icon.py` isolates it
positionally (it sits left of the wordmark) and alpha-keys it off its darkness.
That is why a raster step exists at all.

## Store requirements

Per the Indigo Plugin Developer's Guide: the file must be `Contents/Resources/icon.png`,
optimally 256 × 256, and at least 128px high. Without it the store shows Indigo's
generic plugin icon.

## Provenance and rights

`matter-logo-upstream.svg` is the Matter logo as published on Wikimedia Commons:
<https://commons.wikimedia.org/wiki/File:Logo_of_Matter_connectivity_standard.svg>

Wikimedia holds it as **public domain for copyright purposes** — "This logo image
consists only of simple geometric shapes or text. It does not meet the threshold of
originality needed for copyright protection" — while carrying the standard trademark
notice: "This work includes material that may be protected as a trademark in some
jurisdictions."

Copyright and trademark are separate questions. Matter's logo and wordmark are
**certification marks** administered by the Connectivity Standards Alliance, and
this plugin is **not** CSA-certified (see the PRD's non-goals: no certification,
test attestation certificate). For contrast, Home Assistant may display Matter
branding because it holds two CSA certifications — Home Assistant as a certified
user interface component and the Open Home Foundation Matter Server as a certified
software component.

This plugin is an independent, unofficial integration with no affiliation to, or
endorsement by, the Connectivity Standards Alliance.
