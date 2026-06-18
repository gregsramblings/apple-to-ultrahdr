# apple-to-ultrahdr

Convert Apple HDR photos — **HEIC** *and* **ProRAW (DNG)** — into ISO 21496-1 **"Ultra HDR"** gain-map JPEGs that display in full HDR in **Chrome and Safari**, with **no Apple frameworks**. Runs on Linux, macOS, or Windows.

One command, dispatched by file extension:

```bash
python img2ultrahdr.py photo.heic out.jpg
python img2ultrahdr.py photo.dng  out.jpg
```

By default the highlights are authored to a **1600-nit** ceiling — the peak HDR
brightness of an iPhone 17 Pro Max — so they reach the panel's maximum without
clipping. Override with `--peak-nits` (and `--peak-nits 0` for a faithful,
each-image's-own-headroom render).

The output is an ordinary `.jpg`: it shows a normal SDR image everywhere, and the HDR highlights light up on an HDR display in a supporting browser.

## How it works

| Input | What happens |
| --- | --- |
| `.heic` / `.heif` | Reads Apple's gain map (`apple-hdr-heic` → libheif) and re-encodes it into the ISO 21496-1 log model. |
| `.dng` (ProRAW) | Develops the raw (`rawpy` → LibRaw), keeps Apple's embedded preview as the SDR base, and **recovers the blown highlights from the raw sensor data** into a gain map. |

Both paths converge on **`libultrahdr`** (Google's reference encoder), which packages the SDR base + gain map + Display-P3 ICC into a single ISO 21496-1 Ultra HDR JPEG.

Why bother: Apple's HEIC stores HDR as a proprietary gain map and ProRAW stores it in the raw sensor data — neither is directly web-renderable, and converting them normally requires macOS (Core Image). This does it anywhere.

## Requirements

**System tools** (must be on your `PATH`):

- **libultrahdr** — provides the `ultrahdr_app` encoder
  - macOS: `brew install libultrahdr`
  - Linux: build from [google/libultrahdr](https://github.com/google/libultrahdr) (or install via your package manager if available)
- **exiftool** — used to read Apple's HDR metadata and extract the ProRAW preview
  - macOS: `brew install exiftool`
  - Linux: `apt install libimage-exiftool-perl`

> `rawpy` bundles LibRaw and `apple-hdr-heic` bundles libheif (via pillow-heif), so those don't need separate installs — only `ultrahdr_app` and `exiftool` are external.

**Python** (3.9+):

```bash
pip install -r requirements.txt
```

## Usage

```bash
python img2ultrahdr.py <in.heic|in.heif|in.dng> <out.jpg> [options]
```

Examples:

```bash
# HEIC, default 1600-nit ceiling (iPhone 17 Pro Max peak HDR)
python img2ultrahdr.py IMG_1234.HEIC out.jpg

# ProRAW, same default ceiling
python img2ultrahdr.py IMG_1234.DNG out.jpg

# faithful — each image's own captured headroom (gentlest, no over-drive)
python img2ultrahdr.py IMG_1234.HEIC out.jpg --peak-nits 0

# downscale for the web + a higher ceiling for a brighter XDR/TV display
python img2ultrahdr.py IMG_1234.HEIC out.jpg --maxdim 2400 --peak-nits 4000
```

### Options

**Brightness** (both formats):

| Flag | Effect |
| --- | --- |
| `--max-headroom M` | ceiling as a linear multiplier of SDR white (default: captured/recovered) |
| `--peak-nits N` | ceiling in nits (`M = N / 203`); **default 1600** = iPhone 17 Pro Max peak HDR; `0` = faithful (each image's captured headroom) |
| `--display-headroom D` | `hdrCapacityMax` — display headroom at which the full boost applies (default: match the ceiling, so dimmer displays get a proportional partial boost). Lower = punchier on more displays, but pushes lower-headroom panels toward clipping |

**Output** (both):

| Flag | Effect |
| --- | --- |
| `--maxdim PX` | downscale long edge (`0` = full resolution) |
| `--quality Q` | SDR-base JPEG quality (default `90`) |
| `--sdr PATH` | also write the plain SDR base JPEG |
| `--icc PATH` | Display P3 ICC to embed (default: bundled `DisplayP3.icc`) |

**ProRAW only** (`.dng`):

| Flag | Effect |
| --- | --- |
| `--max-recover M` | cap on headroom pulled from the raw highlights (higher = more dramatic) |
| `--boost-floor F` | SDR luminance below which nothing is boosted (lower = lift more of the scene) |

## Notes

- **Viewing:** the gain map only shows as HDR on an HDR-capable display in **Chrome or Safari** (and Preview/Photos on Apple). On an SDR screen you get the SDR base — that's the built-in fallback.
- **ProRAW is a prototype.** The highlight recovery is tuned with sensible defaults; `--max-recover` / `--boost-floor` let you adjust per scene.
- **Brightness ceiling vs the display:** the default `--peak-nits 1600` targets the iPhone 17 Pro Max's peak HDR brightness, so the brightest highlight lands at the panel's maximum without clipping. Raising it (e.g. `--peak-nits 4000`) authors a higher ceiling for a brighter XDR/TV, but on a 1600-nit phone that over-drives the panel and blows out the highlights. Lower it, or use `--peak-nits 0` (faithful), for a gentler look.
- Lazy imports: the HEIC path needs `apple-hdr-heic`, the DNG path needs `rawpy`.

## License

Not yet specified — private use.
