# apple-to-ultrahdr

Convert Apple HDR photos — **HEIC** *and* **ProRAW (DNG)** — into ISO 21496-1 **"Ultra HDR"** gain-map JPEGs that display in full HDR in **Chrome and Safari**, with **no Apple frameworks**. Runs on Linux, macOS, or Windows.

One command, dispatched by file extension. The output name is optional — leave it
off and the result lands next to the input as `<name>.jpg`, so you can convert a
whole folder at once:

```bash
python img2ultrahdr.py photo.heic          # -> photo.jpg
python img2ultrahdr.py photo.dng           # -> photo.jpg
python img2ultrahdr.py *.heic              # convert every HEIC in the folder
python img2ultrahdr.py photo.heic out.jpg  # or name the output explicitly
```

By default the highlights are authored to a **4000-nit** ceiling with
`hdrCapacityMax` matched to it, so the gain map drives the brightest highlight to
**each display's own peak** — ~1600 nits on an iPhone 17 Pro Max, ~4000 on a
4000-nit XDR/TV, less on a laptop — all from the same JPG, with no clipping.
Override with `--peak-nits` (and `--peak-nits 0` for a faithful,
each-image's-own-headroom render).

The output is an ordinary `.jpg`: it shows a normal SDR image everywhere, and the HDR highlights light up on an HDR display in a supporting browser.

## Why I built this

I'd been looking for a reliable way to turn the `.heic`/`.heif` photos off my
iPhone into HDR JPEGs that actually use the HDR in my phone and my laptop — and
that I could just drop into a web gallery. Like most modern phones, mine captures
a lot more dynamic range than a plain 8-bit JPEG can hold. You really see it in
night shots: street lights, lit windows, and signs that a normal JPEG flattens
into dull patches of white, but that should be *glowing*.

The thing that makes that survive on the web is the **gain map**. The file this
produces is an ordinary `.jpg` — serve it from any gallery, no special format or
hosting. On an SDR screen, or an older browser, you just see the normal 8-bit
photo, so nothing ever breaks. But on an HDR-capable display in a browser that
understands gain maps (Chrome and Safari), the browser reads the extra layer
baked into the file and drives the highlights *past* normal white, up to whatever
that screen can do. Same JPG, one file: it looks right on an old laptop, and the
street lights actually come alive on my phone and my HDR display.

## How it works

| Input | What happens |
| --- | --- |
| `.heic` / `.heif` | Reads Apple's gain map (`apple-hdr-heic` → libheif) and re-encodes it into the ISO 21496-1 log model. |
| `.dng` (ProRAW) | Develops the raw (`rawpy` → LibRaw), keeps Apple's embedded preview as the SDR base, and **recovers the blown highlights from the raw sensor data** into a gain map. |
| `.jpg` / `.jpeg` | Already web-ready — **not re-encoded**. Reports whether it carries a gain map (ISO 21496-1, or the Adobe/Google `hdrgm` format) and passes the file through unchanged. |

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
- **Adobe DNG Converter** *(optional — only for iPhone 17+ ProRAW)* — newer ProRAW is
  DNG 1.7 with a JPEG-XL raw stream the bundled LibRaw can't read. If this free Adobe
  tool is installed, such files are transcoded to a readable DNG automatically.
  - macOS/Windows: [free download from Adobe](https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html) (auto-detected; or set `$DNG_CONVERTER` to its binary)
  - Linux: no native build — JPEG-XL ProRAW can't be converted there yet

> `rawpy` bundles LibRaw and `apple-hdr-heic` bundles libheif (via pillow-heif), so those don't need separate installs — only `ultrahdr_app` and `exiftool` are external (plus Adobe DNG Converter for iPhone 17+ ProRAW).

**Python** (3.9+):

```bash
pip install -r requirements.txt
```

## Usage

```bash
python img2ultrahdr.py <input...> [output.jpg] [options]
```

One or more inputs (`.heic` / `.heif` / `.dng` to convert, `.jpg` / `.jpeg` to
inspect). With no output, each result is written next to its input with a `.jpg`
extension. For a **single** input you can name the output as a trailing `.jpg`
argument or with `-o`; with multiple inputs the output name is always derived, so
globs are never mistaken for an output target.

Examples:

```bash
# HEIC -> IMG_1234.jpg, default 4000-nit ceiling (each display renders to its own peak)
python img2ultrahdr.py IMG_1234.HEIC

# convert every HEIC / ProRAW in the current folder
python img2ultrahdr.py *.heic
python img2ultrahdr.py *.dng

# name the output explicitly (single input)
python img2ultrahdr.py IMG_1234.HEIC out.jpg
python img2ultrahdr.py IMG_1234.HEIC -o out.jpg

# faithful — each image's own captured headroom (gentlest, no over-drive)
python img2ultrahdr.py IMG_1234.HEIC --peak-nits 0

# downscale a whole folder for the web + a lower ceiling capped near a phone's peak
python img2ultrahdr.py *.heic --maxdim 2400 --peak-nits 1600

# JPEG in: not converted — reports whether it has a gain map, copies it through unchanged
python img2ultrahdr.py photo.jpg
```

### Options

**Brightness** (both formats):

| Flag | Effect |
| --- | --- |
| `--max-headroom M` | ceiling as a linear multiplier of SDR white (default: captured/recovered) |
| `--peak-nits N` | ceiling in nits (`M = N / 203`); **default 4000** (with matched `hdrCapacityMax`, each display renders to its own peak); `0` = faithful (each image's captured headroom) |
| `--display-headroom D` | `hdrCapacityMax` — display headroom at which the full boost applies (default: match the ceiling, so dimmer displays get a proportional partial boost). Lower = punchier on more displays, but pushes lower-headroom panels toward clipping |

**Output** (both):

| Flag | Effect |
| --- | --- |
| `-o`, `--output PATH` | output path (single input only); default: input name with a `.jpg` extension |
| `--maxdim PX` | downscale long edge (`0` = full resolution) |
| `--quality Q` | SDR-base JPEG quality (default `90`) |
| `--sdr PATH` | also write the plain SDR base JPEG (single input only) |
| `--icc PATH` | Display P3 ICC to embed (default: bundled `DisplayP3.icc`) |

**ProRAW only** (`.dng`):

| Flag | Effect |
| --- | --- |
| `--max-recover M` | cap on headroom pulled from the raw highlights (higher = more dramatic) |
| `--boost-floor F` | SDR luminance below which nothing is boosted (lower = lift more of the scene) |

## Notes

- **Viewing:** the gain map only shows as HDR on an HDR-capable display in **Chrome or Safari** (and Preview/Photos on Apple). On an SDR screen you get the SDR base — that's the built-in fallback.
- **ProRAW is a prototype.** The highlight recovery is tuned with sensible defaults; `--max-recover` / `--boost-floor` let you adjust per scene.
- **iPhone 17+ ProRAW (DNG 1.7 / JPEG-XL):** the raw stream uses a JPEG-XL codec the bundled LibRaw can't decode. If **Adobe DNG Converter** is installed (see Requirements), the tool transcodes such files to a readable DNG automatically and converts them normally — no extra flags. Without it, only those files fail, with a message telling you to install it; other inputs in the batch still convert.
- **Brightness ceiling vs the display:** the default `--peak-nits 4000` authors a 4000-nit ceiling, and because `hdrCapacityMax` defaults to *match* the ceiling, the brightest highlight renders at **each display's own peak** — ~1600 on an iPhone 17 Pro Max, ~4000 on a 4000-nit XDR/TV, less on a laptop — with no clipping anywhere. Highlights blow out only if you force `--display-headroom` *below* the ceiling (e.g. `--display-headroom 2`), which pushes the full boost onto panels that can't show it. Use `--peak-nits 0` (faithful) if you'd rather each display show the photo's true captured brightness.
- **Batch / output names:** pass as many inputs as you like (e.g. a `*.heic` glob); each output defaults to the input's name with a `.jpg` extension, written alongside it. A custom output name (a trailing `.jpg` argument, or `-o`) is only honored for a **single** input — with multiple inputs the names are always derived, so a glob is never mistaken for an output target. One bad file in a batch is reported and skipped; the rest still convert, and the command exits non-zero if any failed.
- **JPEG passthrough:** a `.jpg` / `.jpeg` input is never re-encoded — JPEG is already a web HDR container. The tool reports whether the file carries a gain map and copies it to the output path **byte-for-byte unchanged** (gain map preserved). Detection uses `exiftool`, looking for the ISO 21496-1 URN or the Adobe/Google `hdrgm` gain-map metadata. If input and output are the same path, the file is left in place.
- Lazy imports: the HEIC path needs `apple-hdr-heic`, the DNG path needs `rawpy`.

## License

[MIT](LICENSE) © 2026 Greg Wilson.

This covers the code in this repository. It runs alongside separately-installed
tools and libraries under their own licenses — notably `libultrahdr` (Apache 2.0)
and `exiftool` (Artistic/GPL) as external binaries, and `apple-hdr-heic` / `rawpy`
(both MIT) as Python dependencies.
