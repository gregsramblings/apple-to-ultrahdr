#!/usr/bin/env python3
"""
img2ultrahdr.py — convert an Apple HDR photo to ISO 21496-1 "Ultra HDR" JPEG
(cross-browser HDR for Chrome + Safari), with NO Apple frameworks.

Dispatches on file extension:
  .heic / .heif  ->  apple-hdr-heic decodes Apple's gain map  -> libultrahdr
  .dng           ->  rawpy/LibRaw recovers blown highlights    -> libultrahdr

Both paths converge on: SDR base + ISO gain map + metadata -> ultrahdr_app.

Usage:
  img2ultrahdr.py <in.heic|in.dng> <out.jpg> [options]

Brightness (both):
  --max-headroom M     ceiling as a linear multiplier of SDR white (default: captured/recovered)
  --peak-nits N        ceiling in nits (M = N / 203; default 1600 = iPhone 17 Pro Max peak HDR; 0 = faithful)
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
import argparse, os, subprocess, tempfile, warnings
warnings.filterwarnings("ignore")
import numpy as np
import cv2

REF_WHITE_NITS = 203.0
# Default brightness ceiling: the iPhone 17 Pro Max peak HDR brightness. Authoring
# above this over-drives the panel (highlights clip → blow out). 1600 / 203 ≈ 7.88x.
DEFAULT_PEAK_NITS = 1600.0


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
        raise SystemExit("no HDR headroom (not an HDR HEIC / gain map missing)")
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
def prepare_dng(path, args):
    """Apple ProRAW DNG -> (sdr_rgb float, iso_recovery float, captured_headroom).

    ProRAW has no usable gain map, so we keep Apple's embedded preview as the SDR
    base and recover the blown highlights from the raw sensor data into the gain map.
    """
    import rawpy
    from PIL import Image, ImageOps

    work = tempfile.mkdtemp()
    prev_path = os.path.join(work, "prev.jpg")
    subprocess.run(["exiftool", "-b", "-PreviewImage", path],
                   stdout=open(prev_path, "wb"), stderr=subprocess.DEVNULL, check=True)
    prev = np.asarray(ImageOps.exif_transpose(Image.open(prev_path)).convert("RGB")).astype(np.float32) / 255.0
    Hp, Wp = prev.shape[:2]

    with rawpy.imread(path) as raw:
        dev = raw.postprocess(gamma=(1, 1), no_auto_bright=True, use_camera_wb=True,
                              output_bps=16, output_color=rawpy.ColorSpace.sRGB)
    dev = dev.astype(np.float32) / 65535.0

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


# -------------------------------------------------------------- shared encode -
def encode(sdr_rgb, iso, captured, args):
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
    subprocess.run(["ultrahdr_app", "-m", "0", "-i", sdr_path, "-g", gm_path, "-f", cfg_path, "-z", args.output],
                   check=True, capture_output=True)
    return max_boost, cap_max, w, h


PREPARE = {".heic": prepare_heic, ".heif": prepare_heic, ".dng": prepare_dng}


def main():
    ap = argparse.ArgumentParser(description="HEIC or ProRAW DNG -> ISO 21496-1 Ultra HDR (Mac-free)")
    ap.add_argument("input"); ap.add_argument("output")
    ap.add_argument("--max-headroom", type=float, default=None)
    ap.add_argument("--peak-nits", type=float, default=DEFAULT_PEAK_NITS,
                    help=f"ceiling in nits, M = N/203 (default {DEFAULT_PEAK_NITS:g} = iPhone 17 Pro Max peak HDR; 0 = faithful/captured)")
    ap.add_argument("--display-headroom", type=float, default=None)
    ap.add_argument("--max-recover", type=float, default=16.0, help="(.dng) cap on recovered headroom")
    ap.add_argument("--boost-floor", type=float, default=0.04, help="(.dng) SDR luminance below which no boost")
    ap.add_argument("--maxdim", type=int, default=0)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--sdr", default=None)
    ap.add_argument("--icc", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "DisplayP3.icc"),
                    help="Display P3 ICC profile to embed (default: bundled DisplayP3.icc)")
    args = ap.parse_args()

    ext = os.path.splitext(args.input)[1].lower()
    prepare = PREPARE.get(ext)
    if prepare is None:
        raise SystemExit(f"unsupported input '{ext}' (expected .heic / .heif / .dng)")

    sdr, iso, captured = prepare(args.input, args)
    max_boost, cap_max, w, h = encode(sdr, iso, captured, args)
    kind = "DNG" if ext == ".dng" else "HEIC"
    print(f"{os.path.basename(args.input)} [{kind}]: {w}x{h}  captured {captured:.2f}x  "
          f"-> ceiling {max_boost:.2f}x (~{round(max_boost * REF_WHITE_NITS)} nits), capMax {cap_max:.2f}x  "
          f"-> {args.output}")


if __name__ == "__main__":
    main()
