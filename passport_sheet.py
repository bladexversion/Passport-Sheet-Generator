#!/usr/bin/env python3
"""
Passport Photo Sheet Maker
--------------------------
Generates print-ready passport photo sheets (3x4 cm photos @ 300 DPI) for:
- 4x6 inch paper (1200x1800 px): 9 photos (3x3 grid)
- A4 paper - Standard (2480x3508 px): 36 photos (6x6 grid, safe print margins)
- A4 paper - Max (2480x3508 px): 42 photos (6x7 grid, max yield)

Supports single photos, folders, custom quantities per photo,
and combined sheets. Output is high-resolution PNG format.
"""

import argparse
import itertools
import os
import sys
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw

VALID_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PHOTO_W, PHOTO_H = 354, 472  # 3x4 cm @ 300 DPI

# Paper configurations at 300 DPI
PAPER_CONFIGS = {
    "4x6": {
        "name": "4 x 6 inch (10 x 15 cm)",
        "width": 1200,
        "height": 1800,
        "cols": 3,
        "rows": 3,
        "capacity": 9,
        "col_xs": [35, 423, 811],     # 3 cols: margins 35px, gaps 34px
        "row_ys": [158, 664, 1170],   # 3 rows: margins 158px, gaps 34px
    },
    "a4": {
        "name": "A4 Paper - Standard (21 x 29.7 cm)",
        "width": 2480,
        "height": 3508,
        "cols": 6,
        "rows": 6,
        "capacity": 36,
        "col_xs": [93 + i * (354 + 34) for i in range(6)],    # margins 93px, gaps 34px
        "row_ys": [253 + i * (472 + 34) for i in range(6)],   # margins 253px, gaps 34px
    },
    "a4-max": {
        "name": "A4 Paper - Maximum (21 x 29.7 cm)",
        "width": 2480,
        "height": 3508,
        "cols": 6,
        "rows": 7,
        "capacity": 42,
        "col_xs": [93 + i * (354 + 34) for i in range(6)],    # margins 93px, gaps 34px
        "row_ys": [42 + i * (472 + 20) for i in range(7)],    # margins 42px, gaps 20px
    }
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate print-ready passport photo sheet(s) on 4x6 or A4 paper."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Path to image file(s) or folder(s).",
    )
    parser.add_argument(
        "--paper",
        type=str,
        choices=["4x6", "a4", "a4-max"],
        default="4x6",
        help="Paper size: '4x6' (9 photos), 'a4' (36 photos), 'a4-max' (42 photos). Default is '4x6'.",
    )
    parser.add_argument(
        "--counts",
        type=int,
        nargs="*",
        default=None,
        help="Custom copy count for each input photo (e.g. --counts 2 3 4).",
    )
    parser.add_argument(
        "--combine",
        action="store_true",
        help="Combine different photos onto sheet(s) instead of creating a separate sheet for each photo.",
    )
    parser.add_argument(
        "--no-repeat",
        action="store_true",
        help="Leave leftover slots blank instead of repeating photos to fill the sheet.",
    )
    parser.add_argument(
        "--bg",
        type=str,
        default="#FFFFFF",
        help="Background color in hex (e.g. #FFFFFF) or standard color name. Default is white.",
    )
    parser.add_argument(
        "--guides",
        type=str,
        choices=["on", "off"],
        default="on",
        help="Draw black cutting borders around each photo. Options: 'on', 'off'. Default is 'on'.",
    )
    parser.add_argument(
        "--border-width",
        type=int,
        default=2,
        help="Border width in pixels for cutting guides (default is 2).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output file path or directory.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Do not automatically open preview after creation.",
    )
    return parser.parse_args()


def collect_image_files(input_paths: list[str]) -> list[Path]:
    """Expands inputs into a sorted list of unique image paths."""
    found_files = []
    for item in input_paths:
        p = Path(item)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS:
                    found_files.append(f)
        elif p.is_file():
            if p.suffix.lower() in VALID_EXTENSIONS:
                found_files.append(p)
            else:
                print(f"[WARNING] Skipping unsupported file type: {p}")
        else:
            print(f"[WARNING] Path not found: {p}")

    seen = set()
    unique_files = []
    for f in found_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(f)
    return unique_files


def load_and_composite(photo_path: Path, bg_color_rgb: tuple) -> Image.Image:
    """Loads input photo and composites transparent areas onto background."""
    try:
        img = Image.open(photo_path)
    except Exception as e:
        print(f"[ERROR] Failed to open image '{photo_path}': {e}", file=sys.stderr)
        raise

    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")

    if img.mode == "RGBA":
        bg_canvas = Image.new("RGBA", img.size, bg_color_rgb + (255,))
        composite = Image.alpha_composite(bg_canvas, img)
        return composite.convert("RGB")
    else:
        return img.convert("RGB")


def crop_and_resize(img: Image.Image, filename: str, target_w: int = PHOTO_W, target_h: int = PHOTO_H) -> Image.Image:
    """Crops to 3:4 aspect ratio centered and resizes using Lanczos."""
    w, h = img.size
    if w < target_w or h < target_h:
        print(f"[WARNING] '{filename}' ({w}x{h} px) is smaller than target size ({target_w}x{target_h} px). Output may look soft.")

    target_ratio = target_w / target_h
    current_ratio = w / h

    if current_ratio > target_ratio:
        new_w = int(round(h * target_ratio))
        left = (w - new_w) // 2
        cropped = img.crop((left, 0, left + new_w, h))
    elif current_ratio < target_ratio:
        new_h = int(round(w / target_ratio))
        top = (h - new_h) // 2
        cropped = img.crop((0, top, w, top + new_h))
    else:
        cropped = img

    resampling = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS)
    return cropped.resize((target_w, target_h), resample=resampling)


def check_layout_bounds(positions, photo_w, photo_h, canvas_w, canvas_h):
    """Verifies that all photos fit inside canvas and do not overlap."""
    rects = []
    for idx, (x, y) in enumerate(positions):
        x2, y2 = x + photo_w, y + photo_h
        if x < 0 or y < 0 or x2 > canvas_w or y2 > canvas_h:
            raise ValueError(f"Photo slot {idx + 1} at ({x}, {y}, {x2}, {y2}) exceeds canvas boundaries.")
        rects.append((x, y, x2, y2))

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            r1, r2 = rects[i], rects[j]
            overlap = not (
                r1[2] <= r2[0] or
                r1[0] >= r2[2] or
                r1[3] <= r2[1] or
                r1[1] >= r2[3]
            )
            if overlap:
                raise ValueError(f"Slots {i + 1} and {j + 1} overlap.")


def build_passport_sheet(
    photos_for_slots: list[Image.Image | None],
    bg_color_rgb: tuple,
    draw_guides: bool = True,
    border_width: int = 2,
    paper_key: str = "4x6"
) -> Image.Image:
    """Creates a print-ready sheet for the specified paper type."""
    config = PAPER_CONFIGS.get(paper_key, PAPER_CONFIGS["4x6"])
    canvas_w = config["width"]
    canvas_h = config["height"]
    positions = [(x, y) for y in config["row_ys"] for x in config["col_xs"]]

    check_layout_bounds(positions, PHOTO_W, PHOTO_H, canvas_w, canvas_h)

    sheet = Image.new("RGB", (canvas_w, canvas_h), bg_color_rgb)
    draw = ImageDraw.Draw(sheet)

    for idx, (x, y) in enumerate(positions):
        photo = photos_for_slots[idx] if idx < len(photos_for_slots) else None
        if photo is not None:
            sheet.paste(photo, (x, y))

        if draw_guides and border_width > 0 and photo is not None:
            draw.rectangle(
                [x, y, x + PHOTO_W - 1, y + PHOTO_H - 1],
                outline=(0, 0, 0),
                width=border_width
            )

    return sheet


def preview_image(image_path: Path):
    """Opens image preview using Windows default viewer."""
    try:
        print(f"Opening preview: {image_path.name}")
        if hasattr(os, "startfile"):
            os.startfile(str(image_path))
        else:
            import subprocess
            subprocess.run(["start", str(image_path)], shell=True)
    except Exception as e:
        print(f"[NOTE] Could not open preview: {e}")


def main():
    args = parse_args()
    paper_key = args.paper
    paper_cfg = PAPER_CONFIGS[paper_key]
    capacity = paper_cfg["capacity"]

    try:
        bg_rgb = ImageColor.getrgb(args.bg)
    except ValueError:
        print(f"[ERROR] Invalid color format '{args.bg}'.", file=sys.stderr)
        sys.exit(1)

    image_paths = collect_image_files(args.inputs)
    if not image_paths:
        print("[ERROR] No valid images found.", file=sys.stderr)
        sys.exit(1)

    total_photos = len(image_paths)
    guides_enabled = (args.guides.lower() == "on")
    generated_files = []

    # Custom copy counts specified
    if args.counts:
        counts = args.counts
        if len(counts) < total_photos:
            counts = counts + [1] * (total_photos - len(counts))
        else:
            counts = counts[:total_photos]
        args.combine = True
    else:
        counts = [1] * total_photos

    # -------------------------------------------------------------
    # CASE 1: Single photo, 1 sheet full of copies
    # -------------------------------------------------------------
    if total_photos == 1 and not args.combine:
        photo_path = image_paths[0]
        print(f"Processing: {photo_path.name} on {paper_cfg['name']} ({capacity} copies)")
        base_img = load_and_composite(photo_path, bg_rgb)
        passport_photo = crop_and_resize(base_img, photo_path.name)

        sheet = build_passport_sheet([passport_photo] * capacity, bg_rgb, draw_guides=guides_enabled, border_width=args.border_width, paper_key=paper_key)

        out_path = Path(args.out) if args.out else Path(f"passport_sheet_{paper_key}.png")
        if out_path.is_dir() or str(args.out or "").endswith(("\\", "/")):
            out_path = out_path / f"passport_sheet_{paper_key}.png"
        elif out_path.suffix.lower() != ".png":
            out_path = out_path.with_suffix(".png")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(out_path), format="PNG", dpi=(300, 300))
        generated_files.append(out_path)
        print(f"[SUCCESS] Saved sheet to: {out_path}")

    # -------------------------------------------------------------
    # CASE 2: Combined Mode / Custom Counts Mode
    # -------------------------------------------------------------
    elif args.combine:
        print(f"Running COMBINED mode on {paper_cfg['name']} (capacity: {capacity} slots)...")

        # Load and expand photos based on requested counts
        expanded_photos = []
        for p, count in zip(image_paths, counts):
            base_img = load_and_composite(p, bg_rgb)
            passport_photo = crop_and_resize(base_img, p.name)
            expanded_photos.extend([passport_photo] * count)

        total_requested = len(expanded_photos)
        out_dir = Path(args.out) if args.out and (Path(args.out).is_dir() or str(args.out).endswith(("\\", "/"))) else Path(".")
        if args.out and not (Path(args.out).is_dir() or str(args.out).endswith(("\\", "/"))):
            base_stem = Path(args.out).stem
            out_dir = Path(args.out).parent
        else:
            base_stem = f"combined_sheet_{paper_key}"

        out_dir.mkdir(parents=True, exist_ok=True)

        num_sheets = (total_requested + capacity - 1) // capacity if total_requested > 0 else 1
        for sheet_idx in range(num_sheets):
            chunk = expanded_photos[sheet_idx * capacity : (sheet_idx + 1) * capacity]
            if not args.no_repeat and len(chunk) < capacity and expanded_photos:
                cycled = itertools.cycle(expanded_photos)
                chunk = [next(cycled) for _ in range(capacity)]

            sheet = build_passport_sheet(chunk, bg_rgb, draw_guides=guides_enabled, border_width=args.border_width, paper_key=paper_key)
            suffix = f"_{sheet_idx + 1}" if num_sheets > 1 else ""
            out_file = out_dir / f"{base_stem}{suffix}.png"
            sheet.save(str(out_file), format="PNG", dpi=(300, 300))
            generated_files.append(out_file)
            print(f"[SUCCESS] Saved sheet {sheet_idx + 1}/{num_sheets}: {out_file.name}")

    # -------------------------------------------------------------
    # CASE 3: Batch Mode (1 full sheet per photo in folder)
    # -------------------------------------------------------------
    else:
        print(f"Running BATCH mode: 1 sheet of {capacity} copies for each of the {total_photos} photos...")
        out_dir = Path(args.out) if args.out else Path(f"output_sheets_{paper_key}")
        out_dir.mkdir(parents=True, exist_ok=True)

        for idx, photo_path in enumerate(image_paths, start=1):
            print(f"[{idx}/{total_photos}] Processing: {photo_path.name}")
            base_img = load_and_composite(photo_path, bg_rgb)
            passport_photo = crop_and_resize(base_img, photo_path.name)

            sheet = build_passport_sheet([passport_photo] * capacity, bg_rgb, draw_guides=guides_enabled, border_width=args.border_width, paper_key=paper_key)
            out_file = out_dir / f"sheet_{paper_key}_{photo_path.stem}.png"
            sheet.save(str(out_file), format="PNG", dpi=(300, 300))
            generated_files.append(out_file)
            print(f"      -> Saved: {out_file.name}")

    # Print reminder
    print("\n" + "=" * 68)
    print("  PRINTING INSTRUCTIONS:")
    print(f"  * Paper Size: {paper_cfg['name']}")
    print("  * Print Scale: Select 'Actual size' or '100%' (NOT 'Fit to page')")
    print("  * Resolution: 300 DPI")
    print("  * Result: Each individual photo cuts out to exactly 3 x 4 cm.")
    print("=" * 68 + "\n")

    if not args.no_preview and generated_files:
        preview_image(generated_files[0])


if __name__ == "__main__":
    main()
