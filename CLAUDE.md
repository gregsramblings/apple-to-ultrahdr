# CLAUDE.md

Guidance for working in this repo. Keep this file current when behavior or
structure changes.

## What this is

A single-file Python CLI ([img2ultrahdr.py](img2ultrahdr.py)) that converts Apple
HDR photos to ISO 21496-1 **Ultra HDR** gain-map JPEGs (web HDR for Chrome/Safari)
with **no Apple frameworks** — so it runs on Linux/Windows, not just macOS. See
[README.md](README.md) for the user-facing docs.

## Architecture (one file, three input paths)

Dispatch is by file extension in `convert_one()`:

- **`.heic` / `.heif`** → `prepare_heic()`: `apple-hdr-heic` (libheif) decodes Apple's
  gain map; it's re-encoded into the ISO log model.
- **`.dng` (ProRAW)** → `prepare_dng()`: `rawpy`/LibRaw develops the raw, keeps Apple's
  embedded preview (`exiftool -PreviewImage`) as the SDR base, and **recovers blown
  highlights from the raw sensor data** into a gain map. This path is a prototype;
  `--max-recover` / `--boost-floor` tune it. **JPEG-XL transcode fallback:** iPhone 17+
  ProRAW is DNG 1.7 with a JPEG-XL raw IFD that the bundled LibRaw can't decode
  (`develop_raw()` raises `rawpy.LibRawError`). On that failure, `transcode_dng()`
  shells out to **Adobe DNG Converter** (`-u -dng1.6 -p2`) to make an uncompressed
  DNG 1.6 LibRaw *can* read, develops that, then deletes it (~300 MB). The SDR base
  still comes from the **original** file's preview (its preview IFD is plain JPEG, so
  exiftool extracts it even when the raw won't decode). Converter is auto-located via
  `find_dng_converter()` ($DNG_CONVERTER → known macOS/Windows paths → PATH); macOS/
  Windows only, so on Linux this raises a clear, actionable `ValueError`.
- **`.jpg` / `.jpeg`** → `handle_jpeg()`: already web-ready, so **never re-encoded**.
  `jpeg_has_gainmap()` reports gain-map status; the file is copied through
  byte-for-byte (skipped when in==out path).

`prepare_heic`/`prepare_dng` both return `(sdr_rgb float, iso_recovery float,
captured_headroom)`, which `encode()` packages via the external `ultrahdr_app`
(libultrahdr) into the final JPEG with a Display-P3 ICC.

## CLI arg resolution — important invariant

`main()` → `plan_jobs(positionals, --output)` turns args into `(input, output)` jobs.
Rules (and the reason they exist):

- Positionals are **all inputs** by default; each output is `<stem>.jpg` next to the
  input. This is what makes `*.heic` batch-convert.
- A trailing `.jpg`/`.jpeg` positional is treated as an **explicit output** only when
  there are exactly 2 positionals **and** the first is a convertible type
  (`.heic/.heif/.dng`). This preserves the legacy `in.heic out.jpg` form **without**
  ever misreading a 2-file glob like `a.heic b.heic` (both convertible → batch) as
  input+output. Do not loosen this without re-checking that glob case — getting it
  wrong silently writes JPEG bytes over an input file.
- `-o/--output` and `--sdr` are single-input only (error otherwise).
- Batch is fault-tolerant: per-file errors are caught, printed to stderr, and the run
  continues; the process exits non-zero if any file failed. For this to work, failures
  in the prepare/encode paths must raise `Exception` (e.g. `ValueError`), **not**
  `SystemExit` (which `except Exception` won't catch). `prepare_heic`'s "no HDR
  headroom" already uses `ValueError` for this reason.

## Gain-map detection

`jpeg_has_gainmap()` shells out to `exiftool` and matches any of these tokens
(lowercased): `21496` (the ISO 21496-1 URN `urn:iso:std:iso:ts:21496:-1`, which is what
libultrahdr/this tool emit in an APP2 marker), `hdrgm` (Adobe/Google XMP namespace), or
`gainmap` (GContainer `GainMap` semantic). Note: libultrahdr's own output is detected via
the **ISO URN**, not `hdrgm` — it stores the gain map as a secondary MPF image and does
not expose an `hdrgm` XMP at the top level. Verify any detection change against a real
`ultrahdr_app -m 0` output, not just Adobe/Google samples.

## Brightness defaults (don't "fix" without understanding)

Default ceiling is `--peak-nits 4000` (`DEFAULT_PEAK_NITS`), and `hdrCapacityMax`
defaults to **match** the ceiling (`cap_max = args.display_headroom or max_boost`). That
matching is deliberate: it makes the brightest highlight render at *each display's own
peak* (~1600 nits on an iPhone 17 Pro Max, ~4000 on an XDR/TV) with no clipping. Setting
`hdrCapacityMax` **below** the ceiling is what causes blowout on lower-headroom panels.
`REF_WHITE_NITS = 203` (SDR reference white) converts nits↔linear multiplier.

## TODO: port the gain-map downscale from filmroll.io (added 2026-07-02)

The copy of this script vendored at `../filmroll.io/container/img2ultrahdr.py` added
optional gain-map shrinking to `encode()` — port it back here:

- `encode()` reads `gm_downscale` / `gm_quality` off the args namespace via
  `getattr(args, "gm_downscale", 1)` / `getattr(args, "gm_quality", 94)` (defaults
  preserve current CLI behavior) and, when `gm_downscale > 1`, stores the gain map at
  1/N of the primary's resolution (`cv2.resize` INTER_AREA) encoded at `gm_quality`.
- Why: the gain map is a smooth low-frequency signal, so 1/2 resolution is visually
  lossless, spec-allowed (renderers upsample; the map's own SOF carries its dims), and
  cuts ~25% off every output file. Verified against `ultrahdr_app` (libultrahdr
  v1.4.0): `-m 0` accepts a smaller-than-primary gain map and the ISO 21496-1 box
  parses identically (checked with filmroll's `_parse_iso21496`).
- A CLI port here should surface it as `--gainmap-downscale` / `--gainmap-quality`
  flags on the argparse namespace (the filmroll copy sets the fields programmatically).

- `ultrahdr_app` (libultrahdr) — the encoder. `brew install libultrahdr`.
- `exiftool` — reads HEIC HDR metadata, extracts the ProRAW preview, and detects JPEG
  gain maps. `brew install exiftool`. The code assumes it is present (no graceful
  fallback if missing).
- **Adobe DNG Converter** (optional) — only needed for JPEG-XL DNGs (iPhone 17+ ProRAW).
  Free download from Adobe; macOS/Windows only. Auto-located by `find_dng_converter()`,
  or set `$DNG_CONVERTER` to its binary. Absence is non-fatal: only JXL DNGs fail (with
  a clear message), and only when one is actually encountered.

`rawpy` bundles LibRaw and `apple-hdr-heic` bundles libheif, so those need no separate
install. Python deps are in [requirements.txt](requirements.txt); HEIC needs
`apple-hdr-heic`, DNG needs `rawpy` (both lazy-imported).

## Running / testing

There is no test suite and the heavy deps are **not** installed in system Python. To run
or test locally, use a venv:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

When validating changes without the full HEIC/DNG decoders installed, you can still:

- unit-test `plan_jobs()` (pure Python — import the module and assert on cases; the
  glob-safety case `["a.heic","b.heic"]` → batch is the one to never regress);
- end-to-end test the JPEG path by building a gain-map fixture with
  `ultrahdr_app -m 0 -i base.jpg -g gm.jpg -f cfg -z uhdr.jpg` and checking the
  passthrough copy is byte-identical.

The HEIC/DNG conversion paths need `apple-hdr-heic` / `rawpy` installed to exercise end
to end.

## Conventions

- Keep it a single script; match the existing terse, comment-explaining-the-why style.
- `.gitignore` excludes image files (`*.jpg/*.heic/*.dng/...`); `DisplayP3.icc` is
  intentionally tracked. Don't commit test images.
- License is MIT ([LICENSE](LICENSE)); the external tools keep their own licenses.
