#!/usr/bin/env python3
"""
img2ultrahdr.py — convert an Apple HDR photo to ISO 21496-1 "Ultra HDR" JPEG
(cross-browser HDR for Chrome + Safari), with NO Apple frameworks.

Dispatches on file extension:
  .heic / .heif  ->  apple-hdr-heic decodes Apple's gain map  -> libultrahdr
  .dng           ->  rawpy/LibRaw recovers blown highlights    -> libultrahdr
  .jpg / .jpeg   ->  already web-ready; report gain-map status, pass through unchanged

The HEIC/DNG paths converge on: SDR base + ISO gain map + metadata -> ultrahdr_app.

Usage:
  img2ultrahdr.py <in...> [out.jpg] [options]

  Takes one or more inputs. With no output, each result is written next to its
  input with a .jpg extension, so `img2ultrahdr.py *.heic` converts every HEIC in
  the folder. For a single input you can name the output as a 2nd argument
  (`in.heic out.jpg`) or with `-o out.jpg`.

Brightness (both):
  --max-headroom M     ceiling as a linear multiplier of SDR white (default: captured/recovered)
  --peak-nits N        ceiling in nits (M = N / 203; default 4000; with matched hdrCapacityMax each display renders to its own peak; 0 = faithful)
  --display-headroom D hdrCapacityMax: display headroom for full boost (default: match the ceiling)
Output (both):
  --maxdim PX          downscale long edge (0 = full res)
  --quality Q          SDR-base JPEG quality (default 90)
  --sdr PATH           also write the plain SDR base JPEG
  --icc PATH           Display P3 ICC to embed
RAW only (.dng):
  --max-recover M      cap on headroom recovered from raw highlights (higher = more dramatic)
  --boost-floor F      SDR luminance below which nothing is boosted (lower = lift more of the scene)
"""
import argparse, os, shutil, subprocess, sys, tempfile, warnings
warnings.filterwarnings("ignore")
import numpy as np
import cv2

REF_WHITE_NITS = 203.0
# Default brightness ceiling. With hdrCapacityMax matched to this ceiling (the
# default whenever --display-headroom is omitted), the gain map drives the
# brightest highlight to *each display's own peak*: ~1600 nits on an iPhone 17
# Pro Max, ~4000 on a 4000-nit XDR/TV, less on a laptop — one JPG, no clipping.
# Highlights only blow out if hdrCapacityMax is set BELOW the ceiling, which
# forces the full boost onto under-powered panels. 4000 / 203 ≈ 19.70x.
DEFAULT_PEAK_NITS = 4000.0


def srgb_eotf(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)

def luminance(rgb):
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


# ---------------------------------------------------------------- HEIC path ---
def prepare_heic(path, args):
    """Apple HDR HEIC -> (sdr_rgb float, iso_recovery float, captured_headroom)."""
    import colour
    from apple_hdr_heic.lib import load_primary_and_aux
    from apple_hdr_heic.metadata import AppleHDRMetadata

    meta = AppleHDRMetadata.from_file(path)
    captured = float(meta.compute_headroom())
    if captured <= 1.0:
        raise ValueError("no HDR headroom (not an HDR HEIC / gain map missing)")
    aux_type = meta.aux_type or "urn:com:apple:photo:2020:aux:hdrgainmap"

    dp3_sdr, gm = load_primary_and_aux(path, aux_type)      # float 0..1, orientation applied
    if gm.ndim == 3:
        gm = gm[..., 0]
    gm = np.clip(cv2.resize(gm, (dp3_sdr.shape[1], dp3_sdr.shape[0]),
                            interpolation=cv2.INTER_LANCZOS4), 0.0, 1.0)
    # Apple's gain-map formula, re-encoded into the ISO log model (gamma=1)
    gain = 1.0 + (captured - 1.0) * colour.models.eotf_sRGB(gm)
    iso = np.clip(np.log2(gain) / np.log2(captured), 0.0, 1.0)
    return dp3_sdr.astype(np.float32), iso.astype(np.float32), captured


# ----------------------------------------------------------------- DNG path ---
# iPhone 17+ ProRAW is DNG 1.7 with a JPEG-XL-compressed raw IFD, which the LibRaw
# bundled in the rawpy wheel can't decode (it fails at postprocess with
# LibRawFileUnsupportedError). If Adobe DNG Converter is installed we transcode such
# a file to an uncompressed DNG 1.6 on the fly — LibRaw reads that fine, and "-u"
# preserves the raw highlight values the gain-map recovery needs. Converter is
# macOS/Windows only (no native Linux build), so on Linux this path raises a clear
# error pointing at the limitation rather than crashing with LibRaw's raw bytes.
DNG_CONVERTER_CANDIDATES = (
    "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter",
    r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
    r"C:\Program Files (x86)\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe",
)


def find_dng_converter():
    """Locate the Adobe DNG Converter binary: $DNG_CONVERTER, then known install
    paths, then PATH. Returns None if not found."""
    env = os.environ.get("DNG_CONVERTER")
    if env:
        return env if os.path.exists(env) else None
    for c in DNG_CONVERTER_CANDIDATES:
        if os.path.exists(c):
            return c
    return shutil.which("Adobe DNG Converter")


def transcode_dng(path, work):
    """Transcode a LibRaw-unreadable DNG (e.g. iPhone 17 JPEG-XL) into an
    uncompressed DNG 1.6 under `work`, returning the new path. Raises ValueError
    (caught by the batch runner) if the converter is missing or produces nothing."""
    conv = find_dng_converter()
    if not conv:
        raise ValueError(
            "LibRaw can't decode this DNG (likely DNG 1.7 / JPEG-XL from an "
            "iPhone 17 or newer). Install Adobe DNG Converter, or point "
            "$DNG_CONVERTER at its binary, so it can be transcoded first.")
    out_dir = os.path.join(work, "transcoded")
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run([conv, "-u", "-dng1.6", "-p2", "-d", out_dir, path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    outs = [f for f in os.listdir(out_dir) if f.lower().endswith(".dng")]
    if not outs:
        raise ValueError("Adobe DNG Converter ran but produced no output DNG")
    return os.path.join(out_dir, outs[0])


def develop_raw(path):
    """rawpy/LibRaw develop: linear (gamma 1,1), camera WB, no auto-bright, 16-bit
    sRGB, returned as float 0..1."""
    import rawpy
    with rawpy.imread(path) as raw:
        dev = raw.postprocess(gamma=(1, 1), no_auto_bright=True, use_camera_wb=True,
                              output_bps=16, output_color=rawpy.ColorSpace.sRGB)
    return dev.astype(np.float32) / 65535.0


def prepare_dng(path, args):
    """Apple ProRAW DNG -> (sdr_rgb float, iso_recovery float, captured_headroom).

    ProRAW has no usable gain map, so we keep Apple's embedded preview as the SDR
    base and recover the blown highlights from the raw sensor data into the gain map.
    """
    import rawpy
    from PIL import Image, ImageOps

    work = tempfile.mkdtemp()
    prev_path = os.path.join(work, "prev.jpg")
    # Keep Apple's *original* embedded preview as the SDR base (the JPEG-XL DNG's
    # preview IFD is plain JPEG, so exiftool extracts it even when LibRaw can't
    # read the raw); only the raw develop below needs the transcode fallback.
    subprocess.run(["exiftool", "-b", "-PreviewImage", path],
                   stdout=open(prev_path, "wb"), stderr=subprocess.DEVNULL, check=True)
    prev = np.asarray(ImageOps.exif_transpose(Image.open(prev_path)).convert("RGB")).astype(np.float32) / 255.0
    Hp, Wp = prev.shape[:2]

    try:
        dev = develop_raw(path)
    except rawpy.LibRawError:
        tdng = transcode_dng(path, work)   # iPhone 17 JPEG-XL DNG -> uncompressed DNG 1.6
        try:
            dev = develop_raw(tdng)
        finally:
            shutil.rmtree(os.path.dirname(tdng), ignore_errors=True)  # the transcode is ~300 MB

    # orient the RAW to match the preview (pick the rotation that best correlates)
    if dev.shape[:2] != (Hp, Wp):
        cands = [np.rot90(dev, 1), np.rot90(dev, 3)] if dev.shape[:2] == (Wp, Hp) else [dev]
        def corr(a):
            la = cv2.resize(luminance(a), (64, 64)); lp = cv2.resize(luminance(prev), (64, 64))
            return np.corrcoef(la.ravel(), lp.ravel())[0, 1]
        dev = max(cands, key=corr)
    if dev.shape[:2] != (Hp, Wp):
        dev = cv2.resize(dev, (Wp, Hp), interpolation=cv2.INTER_AREA)

    prev_lin = srgb_eotf(prev); prev_lum = luminance(prev_lin); raw_lum = luminance(dev)
    mask = (prev_lum > 0.02) & (prev_lum < 0.6) & (raw_lum > 1e-5)
    scale = float(np.median(prev_lum[mask] / raw_lum[mask]))     # align RAW exposure to preview
    dev_aligned = dev * scale

    k = 0.02   # offset stops near-zero channels (e.g. a pure-red lantern's green) from exploding
    gain = np.maximum(np.max((dev_aligned + k) / (prev_lin + k), axis=2), 1.0)
    def smoothstep(x, a, b):
        t = np.clip((x - a) / (b - a), 0.0, 1.0); return t * t * (3 - 2 * t)
    gain = 1.0 + (gain - 1.0) * smoothstep(prev_lum, args.boost_floor, args.boost_floor + 0.08)
    gain = cv2.GaussianBlur(gain, (0, 0), 1.2)
    captured = float(min(np.percentile(gain, 99.9), args.max_recover))
    gain = np.clip(gain, 1.0, captured)
    iso = np.clip(np.log2(gain) / np.log2(captured), 0.0, 1.0)
    return prev.astype(np.float32), iso.astype(np.float32), captured


# ---------------------------------------------------------------- JPEG path ---
def jpeg_has_gainmap(path):
    """True if the JPEG already carries a gain map. exiftool surfaces one of:
      - the ISO 21496-1 URN `urn:iso:std:iso:ts:21496:-1` (what libultrahdr and this
        tool emit), in an APP2 marker;
      - the Adobe/Google `hdrgm` XMP namespace, or a GContainer directory item with a
        `GainMap` semantic (older Ultra HDR / Lightroom HDR).
    -ee also reaches a gain map carried as the secondary MPF image."""
    out = subprocess.run(["exiftool", "-ee", "-G1", "-s", "-a", path],
                         capture_output=True, text=True, errors="ignore").stdout.lower()
    return any(tok in out for tok in ("21496", "hdrgm", "gainmap"))


def handle_jpeg(in_path, out_path):
    """A JPEG is already web-ready, so we never re-encode it: report whether it
    carries a gain map and pass the file through unchanged."""
    name = os.path.basename(in_path)
    if os.path.abspath(in_path) != os.path.abspath(out_path):
        shutil.copyfile(in_path, out_path)
    if jpeg_has_gainmap(in_path):
        print(f"{name} [JPEG]: already has a gain map — kept as-is -> {out_path}")
    else:
        print(f"{name} [JPEG]: not HDR, contains no gain map — kept as-is -> {out_path}")


# -------------------------------------------------------------- shared encode -
def encode(sdr_rgb, iso, captured, args, out_path):
    from PIL import Image
    h, w = sdr_rgb.shape[:2]
    if args.maxdim and max(h, w) > args.maxdim:
        s = args.maxdim / max(h, w); w, h = round(w * s), round(h * s)
        sdr_rgb = cv2.resize(sdr_rgb, (w, h), interpolation=cv2.INTER_AREA)
        iso = cv2.resize(iso, (w, h), interpolation=cv2.INTER_AREA)

    max_boost = args.max_headroom or (args.peak_nits / REF_WHITE_NITS if args.peak_nits else captured)
    cap_max = args.display_headroom or max_boost
    max_boost, cap_max = max(max_boost, 1.0001), max(cap_max, 1.0001)

    work = tempfile.mkdtemp()
    icc = open(args.icc, "rb").read()
    sdr_path, gm_path, cfg_path = (os.path.join(work, n) for n in ("sdr.jpg", "gm.jpg", "m.cfg"))
    img = Image.fromarray((np.clip(sdr_rgb, 0, 1) * 255).astype(np.uint8), "RGB")
    img.save(sdr_path, "JPEG", quality=args.quality, icc_profile=icc)
    if args.sdr:
        img.save(args.sdr, "JPEG", quality=args.quality, icc_profile=icc)
    cv2.imwrite(gm_path, (np.clip(iso, 0, 1) * 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 94])
    open(cfg_path, "w").write(
        f"--maxContentBoost {max_boost:.6f}\n--minContentBoost 1.0\n--gamma 1.0\n--offsetSdr 0.0\n--offsetHdr 0.0\n"
        f"--hdrCapacityMin 1.0\n--hdrCapacityMax {cap_max:.6f}\n--useBaseColorSpace 1\n")
    subprocess.run(["ultrahdr_app", "-m", "0", "-i", sdr_path, "-g", gm_path, "-f", cfg_path, "-z", out_path],
                   check=True, capture_output=True)
    return max_boost, cap_max, w, h


PREPARE = {".heic": prepare_heic, ".heif": prepare_heic, ".dng": prepare_dng}


def default_output(in_path):
    """Output path next to the input, with the extension swapped for .jpg."""
    return os.path.splitext(in_path)[0] + ".jpg"


def convert_one(in_path, out_path, args):
    """Convert/inspect a single file. Raises ValueError on unsupported input so a
    batch run can report it and carry on with the rest."""
    ext = os.path.splitext(in_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        handle_jpeg(in_path, out_path)
        return
    prepare = PREPARE.get(ext)
    if prepare is None:
        raise ValueError(f"unsupported input '{ext}' (expected .heic / .heif / .dng / .jpg)")
    sdr, iso, captured = prepare(in_path, args)
    max_boost, cap_max, w, h = encode(sdr, iso, captured, args, out_path)
    kind = "DNG" if ext == ".dng" else "HEIC"
    print(f"{os.path.basename(in_path)} [{kind}]: {w}x{h}  captured {captured:.2f}x  "
          f"-> ceiling {max_boost:.2f}x (~{round(max_boost * REF_WHITE_NITS)} nits), capMax {cap_max:.2f}x  "
          f"-> {out_path}")


def plan_jobs(positionals, explicit_output):
    """Resolve positional args into a list of (input, output) jobs.

    All positionals are inputs by default, each written to its own `<name>.jpg`
    (so `*.heic` batch-converts). Two backward-compatible single-file forms name
    the output explicitly: `in.heic out.jpg` (a 2nd positional whose extension is
    .jpg/.jpeg, with a convertible first input) or `-o out.jpg`. The 2nd-positional
    form deliberately requires the trailing arg to be a .jpg/.jpeg so a glob like
    `a.heic b.heic` is never mistaken for input+output."""
    if explicit_output is not None:
        if len(positionals) != 1:
            raise SystemExit("-o/--output takes a single input file")
        return [(positionals[0], explicit_output)]
    if (len(positionals) == 2
            and os.path.splitext(positionals[0])[1].lower() in PREPARE
            and os.path.splitext(positionals[1])[1].lower() in (".jpg", ".jpeg")):
        return [(positionals[0], positionals[1])]
    return [(p, default_output(p)) for p in positionals]


def main():
    ap = argparse.ArgumentParser(description="HEIC / ProRAW DNG -> ISO 21496-1 Ultra HDR (Mac-free)")
    ap.add_argument("inputs", nargs="+", metavar="input",
                    help="one or more inputs (.heic/.heif/.dng to convert, .jpg/.jpeg to inspect); "
                         "globs allowed. A single trailing .jpg is taken as the output path")
    ap.add_argument("-o", "--output", default=None,
                    help="output path (single input only); default: input name with a .jpg extension")
    ap.add_argument("--max-headroom", type=float, default=None)
    ap.add_argument("--peak-nits", type=float, default=DEFAULT_PEAK_NITS,
                    help=f"ceiling in nits, M = N/203 (default {DEFAULT_PEAK_NITS:g}; with matched hdrCapacityMax each display renders to its own peak; 0 = faithful/captured)")
    ap.add_argument("--display-headroom", type=float, default=None)
    ap.add_argument("--max-recover", type=float, default=16.0, help="(.dng) cap on recovered headroom")
    ap.add_argument("--boost-floor", type=float, default=0.04, help="(.dng) SDR luminance below which no boost")
    ap.add_argument("--maxdim", type=int, default=0)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--sdr", default=None)
    ap.add_argument("--icc", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "DisplayP3.icc"),
                    help="Display P3 ICC profile to embed (default: bundled DisplayP3.icc)")
    args = ap.parse_args()

    jobs = plan_jobs(args.inputs, args.output)
    if args.sdr and len(jobs) != 1:
        raise SystemExit("--sdr takes a single input file")

    failures = 0
    for in_path, out_path in jobs:
        try:
            convert_one(in_path, out_path, args)
        except Exception as e:
            failures += 1
            print(f"{os.path.basename(in_path)}: ERROR: {e}", file=sys.stderr)
    if failures:
        raise SystemExit(f"{failures} of {len(jobs)} file(s) failed")


if __name__ == "__main__":
    main()
