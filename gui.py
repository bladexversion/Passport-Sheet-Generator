#!/usr/bin/env python3
"""
Passport Photo Sheet Maker - Modern Windows 11 Desktop Application
------------------------------------------------------------------
Bug-fixed standalone desktop app.
Fixes applied:
  1. viewSheet() missing closing brace (caused all global JS to be trapped inside it)
  2. generateSheet() now calls fetchState() after success (fresh thumbnail/allocation)
  3. STATE_LOCK released before heavy image processing (no more frozen UI during generate)
  4. fill_evenly division-by-zero when no photos selected
  5. Default copy count changed from 3 to 1
  6. Border-width stepper allows 1-10 px in UI (was capped at 6)
  7. Auto-fill toggle debounced against focus/visibility race condition
  8. Drag-and-drop folder reader loops readEntries() until exhausted (handles 100+ files)
  9. Graceful server shutdown: socket released before process exits
"""

import base64
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw
import io

# Import core passport sheet engine
import passport_sheet
from passport_sheet import (
    PHOTO_W,
    PHOTO_H,
    PAPER_CONFIGS,
    VALID_EXTENSIONS,
    build_passport_sheet,
    load_and_composite,
    crop_and_resize,
    collect_image_files
)

# Application State
APP_STATE = {
    "paper": "a4-max",               # "4x6", "a4", "a4-max"
    "bg": "#FFFFFF",                 # Background color
    "guides": True,                  # Cutting borders enabled
    "border_width": 2,               # Default cutting border thickness in px
    "auto_fill": True,               # Auto-fill free slots
    "photos": [],                    # List of photo objects
    "latest_file": None,
    "latest_dir": None,
    "last_ping": time.time()
}

STATE_LOCK = threading.Lock()


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get_file_metadata(path: Path) -> dict:
    """Returns size and dimensions of an image file."""
    try:
        sz_bytes = path.stat().st_size
        sz_mb = sz_bytes / (1024 * 1024)
        size_str = f"{sz_mb:.1f} MB" if sz_mb >= 0.1 else f"{max(1, sz_bytes // 1024)} KB"

        ext = path.suffix.upper().replace(".", "")
        if ext == "JPEG":
            ext = "JPG"

        with Image.open(path) as img:
            w, h = img.size
            dim_str = f"{w} \u00d7 {h}"

        return {
            "ext": ext,
            "meta_str": f"{ext} \u2022 {size_str} \u2022 {dim_str}"
        }
    except Exception:
        return {"ext": "IMG", "meta_str": "Image File"}


def init_photos_from_folder():
    """Auto-populates photos from the PHOTOS folder or current directory."""
    cwd = Path.cwd()
    found = []

    # Check PHOTOS subfolder first
    photos_dir = cwd / "PHOTOS"
    if photos_dir.is_dir():
        for f in sorted(photos_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS:
                found.append(f)

    # Also check current directory
    if not found:
        for f in sorted(cwd.iterdir()):
            if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS:
                found.append(f)

    with STATE_LOCK:
        APP_STATE["photos"] = []
        for idx, p in enumerate(found):
            meta = get_file_metadata(p)
            APP_STATE["photos"].append({
                "id": idx,
                "path": str(p.resolve()),
                "name": p.name,
                "selected": True,
                "count": 1,          # FIX: default 1 copy (was 3)
                "meta_str": meta["meta_str"]
            })


def calculate_allocation():
    with STATE_LOCK:
        paper_key = APP_STATE["paper"]
        cap = PAPER_CONFIGS[paper_key]["capacity"]
        total = sum(p["count"] for p in APP_STATE["photos"] if p["selected"])
        empty = max(0, cap - total)
        return {
            "capacity": cap,
            "allocated": total,
            "empty": empty
        }


# ---------------------------------------------------------------------------
# HTML, CSS & JavaScript UI
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Passport Photo Sheet Maker</title>
<style>
  :root {
    --bg-page: #F8FAFC;
    --card-bg: #FFFFFF;
    --border-color: #E2E8F0;
    --border-active: #2563EB;
    --text-primary: #0F172A;
    --text-secondary: #64748B;
    --text-muted: #94A3B8;
    --primary-blue: #2563EB;
    --primary-hover: #1D4ED8;
    --blue-tint: #EFF6FF;
    --red-text: #DC2626;
    --red-border: #FECACA;
    --red-hover: #FEF2F2;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", Roboto, sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; font-family: var(--font); }

  html, body {
    background: var(--bg-page);
    color: var(--text-primary);
    min-width: 0;
    -webkit-font-smoothing: antialiased;
    user-select: none;
    overflow-x: auto;
  }

  body {
    padding: 16px 24px 32px;
  }

  .container {
    max-width: 1060px;
    margin: 0 auto;
    position: relative;
  }

  /* Full Screen Drag & Drop Overlay */
  .drag-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(239, 246, 255, 0.95);
    border: 3px dashed var(--primary-blue);
    z-index: 99999;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    pointer-events: none;
    backdrop-filter: blur(2px);
  }

  .drag-overlay.active { display: flex; }

  .drag-overlay-icon svg {
    width: 64px;
    height: 64px;
    stroke: var(--primary-blue);
    stroke-width: 2;
    fill: none;
  }

  .drag-overlay-title {
    font-size: 20px;
    font-weight: 700;
    color: var(--primary-blue);
  }

  .drag-overlay-sub {
    font-size: 13.5px;
    color: var(--text-secondary);
  }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 16px;
  }

  .app-icon {
    width: 42px;
    height: 42px;
    background: var(--primary-blue);
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 6px rgba(37,99,235,0.25);
    flex-shrink: 0;
  }

  .app-icon svg { width: 24px; height: 24px; fill: #FFFFFF; }

  .header-text h1 {
    font-size: 19px;
    font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.2px;
  }

  .header-text p {
    font-size: 12.5px;
    color: var(--text-secondary);
    margin-top: 1px;
  }

  /* Card Sections */
  .section-card {
    background: var(--card-bg);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.02);
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
    gap: 12px;
    flex-wrap: wrap;
  }

  .card-title-left {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .step-badge {
    width: 24px;
    height: 24px;
    background: var(--primary-blue);
    color: #FFFFFF;
    font-size: 12.5px;
    font-weight: 700;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .step-icon svg {
    width: 18px;
    height: 18px;
    stroke: var(--primary-blue);
    stroke-width: 2;
    fill: none;
    vertical-align: middle;
  }

  .card-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--text-primary);
  }

  /* Step 1: Paper Cards Grid with Responsive Wrapping */
  .paper-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
  }

  .paper-card {
    border: 1px solid var(--border-color);
    border-radius: 9px;
    padding: 13px 16px;
    background: #FFFFFF;
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    transition: all 0.15s ease;
    position: relative;
    min-width: 0;
  }

  .paper-card:hover {
    border-color: #CBD5E1;
  }

  .paper-card.active {
    border: 2px solid var(--primary-blue);
    background: #FFFFFF;
    padding: 12px 15px;
  }

  .radio-circle {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 2px solid #94A3B8;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .paper-card.active .radio-circle {
    border-color: var(--primary-blue);
  }

  .radio-circle-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--primary-blue);
    display: none;
  }

  .paper-card.active .radio-circle-dot {
    display: block;
  }

  .paper-info {
    min-width: 0;
    padding-right: 70px;
  }

  .paper-info .title {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .paper-card.active .title-highlight {
    color: var(--primary-blue);
  }

  .paper-info .desc {
    font-size: 11.5px;
    color: var(--text-secondary);
    margin-top: 1px;
  }

  .badge-recommended {
    position: absolute;
    top: 10px;
    right: 12px;
    background: var(--blue-tint);
    color: var(--primary-blue);
    border: 1px solid #BFDBFE;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    gap: 3px;
    white-space: nowrap;
  }

  /* Step 2: Buttons & Table */
  .action-buttons {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .btn-outline {
    background: #FFFFFF;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text-primary);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s;
    white-space: nowrap;
  }

  .btn-outline:hover { background: #F8FAFC; border-color: #CBD5E1; }

  .photo-table-wrap {
    overflow-x: auto;
    margin-top: 10px;
    border-top: 1px solid var(--border-color);
  }

  .photo-table {
    min-width: 560px;
  }

  .table-header {
    display: flex;
    align-items: center;
    padding: 10px 6px;
    font-size: 12.5px;
    color: var(--text-secondary);
    font-weight: 600;
    border-bottom: 1px solid #F1F5F9;
  }

  .th-check { width: 34px; display: flex; align-items: center; flex-shrink: 0; }
  .th-name { flex: 1; min-width: 140px; }
  .th-copies { width: 180px; flex-shrink: 0; }
  .th-action { width: 90px; text-align: right; flex-shrink: 0; }

  .btn-clear-all {
    background: #FFFFFF;
    border: 1px solid var(--red-border);
    color: var(--red-text);
    padding: 3px 8px;
    border-radius: 6px;
    font-size: 11.5px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
  }

  .btn-clear-all:hover { background: var(--red-hover); }

  .photo-row {
    display: flex;
    align-items: center;
    padding: 9px 6px;
    border-bottom: 1px solid #F8FAFC;
    transition: background 0.15s;
  }

  .photo-row:hover { background: #FAFAFA; }

  .td-check { width: 34px; display: flex; align-items: center; flex-shrink: 0; }

  /* Custom Checkbox */
  .checkbox-custom {
    width: 17px;
    height: 17px;
    border-radius: 4px;
    border: 1.5px solid #94A3B8;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #FFFFFF;
    flex-shrink: 0;
  }

  .checkbox-custom.checked {
    background: var(--primary-blue);
    border-color: var(--primary-blue);
  }

  .checkbox-custom svg {
    width: 11px;
    height: 11px;
    stroke: #FFFFFF;
    stroke-width: 2.5;
    fill: none;
    display: none;
  }

  .checkbox-custom.checked svg { display: block; }

  .td-thumb {
    width: 44px;
    height: 56px;
    border-radius: 5px;
    object-fit: cover;
    margin-right: 12px;
    border: 1px solid #E2E8F0;
    background: #E2E8F0;
    flex-shrink: 0;
  }

  .td-info {
    flex: 1;
    min-width: 100px;
    padding-right: 12px;
    overflow: hidden;
  }

  .td-name {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .td-meta {
    font-size: 11.5px;
    color: var(--text-secondary);
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Stepper */
  .td-copies {
    width: 180px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }

  .copies-label {
    font-size: 12.5px;
    color: var(--text-secondary);
  }

  .stepper {
    display: flex;
    align-items: center;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: #FFFFFF;
    overflow: hidden;
  }

  .stepper-btn {
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #FFFFFF;
    border: none;
    cursor: pointer;
    font-size: 14px;
    color: #475569;
    transition: background 0.15s;
  }

  .stepper-btn:hover { background: #F1F5F9; color: #0F172A; }

  .stepper-val {
    width: 32px;
    text-align: center;
    font-size: 13px;
    font-weight: 700;
    color: var(--text-primary);
  }

  /* Remove button */
  .td-action {
    width: 90px;
    text-align: right;
    flex-shrink: 0;
  }

  .btn-remove {
    background: #FFFFFF;
    border: 1px solid var(--red-border);
    color: var(--red-text);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: background 0.15s;
  }

  .btn-remove:hover { background: var(--red-hover); }

  /* Empty State Drop Zone */
  .empty-drop-zone {
    padding: 36px 20px;
    text-align: center;
    border: 2px dashed #CBD5E1;
    border-radius: 10px;
    margin: 14px 6px;
    background: #FAFAFA;
    cursor: pointer;
  }

  .empty-drop-zone:hover {
    border-color: var(--primary-blue);
    background: var(--blue-tint);
  }

  .empty-drop-zone p {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--text-primary);
    margin-top: 6px;
  }

  .empty-drop-zone span {
    font-size: 12px;
    color: var(--text-secondary);
  }

  /* Table Footer / Allocation bar */
  .card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 12px;
    padding-top: 10px;
    gap: 12px;
    flex-wrap: wrap;
  }

  .pill-allocated {
    background: var(--blue-tint);
    color: var(--primary-blue);
    border: 1px solid #BFDBFE;
    font-size: 12.5px;
    font-weight: 700;
    padding: 5px 14px;
    border-radius: 20px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
  }

  .toggle-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    white-space: nowrap;
  }

  .toggle-switch {
    width: 38px;
    height: 22px;
    background: #CBD5E1;
    border-radius: 12px;
    position: relative;
    transition: background 0.2s;
    flex-shrink: 0;
  }

  .toggle-switch.on { background: var(--primary-blue); }

  .toggle-thumb {
    width: 18px;
    height: 18px;
    background: #FFFFFF;
    border-radius: 50%;
    position: absolute;
    top: 2px;
    left: 2px;
    transition: transform 0.2s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.15);
  }

  .toggle-switch.on .toggle-thumb {
    transform: translateX(16px);
  }

  .toggle-label {
    font-size: 12.5px;
    font-weight: 500;
    color: #334155;
  }

  /* Step 3: Background & Border */
  .bg-options-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
  }

  .bg-left {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .bg-title {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .color-pills {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .pill-color {
    border: 1px solid var(--border-color);
    background: #FFFFFF;
    border-radius: 7px;
    padding: 6px 14px;
    font-size: 12.5px;
    font-weight: 600;
    color: #334155;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: all 0.15s;
    white-space: nowrap;
  }

  .pill-color:hover { border-color: #94A3B8; }

  .pill-color.active {
    border: 2px solid var(--primary-blue);
    color: var(--text-primary);
    padding: 5px 13px;
  }

  .pill-radio {
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 1.5px solid #94A3B8;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .pill-color.active .pill-radio {
    border-color: var(--primary-blue);
  }

  .pill-radio-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--primary-blue);
    display: none;
  }

  .pill-color.active .pill-radio-dot { display: block; }

  /* Cutting Borders Setting with Pixel Width Stepper */
  .guides-setting-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    background: #F8FAFC;
    padding: 5px 12px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
  }

  .guides-checkbox-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    white-space: nowrap;
  }

  .guides-label {
    font-size: 12.5px;
    font-weight: 600;
    color: #1E293B;
  }

  .border-width-picker {
    display: flex;
    align-items: center;
    gap: 6px;
    border-left: 1px solid #CBD5E1;
    padding-left: 10px;
  }

  .border-picker-label {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .border-stepper {
    display: flex;
    align-items: center;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background: #FFFFFF;
    overflow: hidden;
  }

  .border-step-btn {
    width: 22px;
    height: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #FFFFFF;
    border: none;
    cursor: pointer;
    font-size: 13px;
    color: #475569;
  }

  .border-step-btn:hover { background: #F1F5F9; color: #0F172A; }

  .border-step-val {
    width: 34px;
    text-align: center;
    font-size: 12px;
    font-weight: 700;
    color: var(--primary-blue);
  }

  /* Mini Graphic Illustration on Right */
  .preview-diagram {
    display: flex;
    align-items: center;
    gap: 10px;
    padding-left: 10px;
    flex-shrink: 0;
  }

  .diag-photo {
    width: 32px;
    height: 42px;
    border-radius: 4px;
    object-fit: cover;
    border: 1px solid #CBD5E1;
  }

  .diag-arrow svg {
    width: 22px;
    height: 16px;
    stroke: var(--primary-blue);
    stroke-width: 2;
    fill: none;
  }

  .diag-sheet {
    width: 48px;
    height: 44px;
    background: #FFFFFF;
    border: 2px solid #0F172A;
    border-radius: 4px;
    padding: 3px;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-gap: 2px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }

  .diag-cell {
    background: #3B82F6;
    border-radius: 1px;
    height: 7px;
  }

  /* Big Primary Button */
  .btn-generate {
    width: 100%;
    background: var(--primary-blue);
    color: #FFFFFF;
    border: none;
    border-radius: 9px;
    padding: 13px;
    font-size: 14.5px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    margin-bottom: 14px;
    box-shadow: 0 2px 6px rgba(37,99,235,0.22);
    transition: background 0.15s, transform 0.05s;
  }

  .btn-generate:hover { background: var(--primary-hover); }
  .btn-generate:active { transform: translateY(1px); }

  .btn-generate svg { width: 19px; height: 19px; fill: #FFFFFF; }

  /* Print Instructions Footer Card */
  .footer-card {
    background: var(--card-bg);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 13px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
  }

  .footer-left {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }

  .footer-icon svg {
    width: 19px;
    height: 19px;
    stroke: #475569;
    stroke-width: 2;
    fill: none;
    margin-top: 1px;
    flex-shrink: 0;
  }

  .footer-title {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .footer-bullets {
    font-size: 11.5px;
    color: #475569;
    line-height: 1.5;
    margin-top: 2px;
  }

  .footer-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  /* Toast Notification */
  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: #0F172A;
    color: #FFFFFF;
    padding: 10px 18px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    box-shadow: 0 4px 12px rgba(0,0,0,0.18);
    display: none;
    z-index: 100000;
    animation: fadeIn 0.2s;
    max-width: 340px;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  /* Spinner overlay for generate */
  .spinner-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(248,250,252,0.75);
    z-index: 99998;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    backdrop-filter: blur(2px);
  }
  .spinner-overlay.active { display: flex; }
  .spinner {
    width: 44px; height: 44px;
    border: 4px solid #E2E8F0;
    border-top-color: var(--primary-blue);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner-label { font-size: 14px; font-weight: 600; color: var(--text-secondary); }

  /* Responsive tweaks */
  @media (max-width: 600px) {
    body { padding: 10px 10px 24px; }
    .paper-info { padding-right: 4px; }
    .badge-recommended { display: none; }
    .th-copies, .td-copies { width: 130px; }
    .th-action, .td-action { width: 70px; }
    .btn-remove { padding: 4px 6px; font-size: 11px; }
    .photo-table { min-width: 440px; }
  }
</style>
</head>
<body>

<!-- Hidden File and Folder Pickers (Native Windows Dialogs) -->
<input type="file" id="native-file-picker" multiple accept=".png,.jpg,.jpeg,.webp,.bmp,.tiff,.tif,image/*" style="display:none">
<input type="file" id="native-folder-picker" webkitdirectory directory style="display:none">

<!-- Drag & Drop Full Window Visual Overlay -->
<div class="drag-overlay" id="drag-overlay">
  <div class="drag-overlay-icon">
    <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
  </div>
  <div class="drag-overlay-title">Drop Photos or Folders Here</div>
  <div class="drag-overlay-sub">Release mouse to add photos to the passport sheet queue</div>
</div>

<!-- Spinner shown while generating -->
<div class="spinner-overlay" id="spinner-overlay">
  <div class="spinner"></div>
  <div class="spinner-label">Generating passport sheet&hellip;</div>
</div>

<div class="container">

  <!-- App Header -->
  <div class="header">
    <div class="app-icon">
      <svg viewBox="0 0 24 24"><path d="M4 4h3l2-2h6l2 2h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm8 14a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm0-2a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/></svg>
    </div>
    <div class="header-text">
      <h1>Passport Photo Sheet Maker</h1>
      <p>Print 3&times;4 cm passport photos at 300 DPI on 4&times;6&rdquo; or standard A4 paper.</p>
    </div>
  </div>

  <!-- Step 1: Paper Size & Capacity -->
  <div class="section-card">
    <div class="card-header">
      <div class="card-title-left">
        <div class="step-badge">1</div>
        <div class="step-icon">
          <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        </div>
        <div class="card-title">Paper Size &amp; Capacity</div>
      </div>
    </div>
    <div class="paper-grid">
      <!-- 4x6 -->
      <div class="paper-card" id="card-4x6" onclick="setPaper('4x6')">
        <div class="radio-circle"><div class="radio-circle-dot"></div></div>
        <div class="paper-info">
          <div class="title">4 x 6 inch <span class="title-highlight">(9 photos)</span></div>
          <div class="desc">Standard Photo Paper</div>
        </div>
      </div>
      <!-- A4 36 -->
      <div class="paper-card" id="card-a4" onclick="setPaper('a4')">
        <div class="radio-circle"><div class="radio-circle-dot"></div></div>
        <div class="paper-info">
          <div class="title">A4 Paper <span class="title-highlight">(36 photos)</span></div>
          <div class="desc">Safe Margins</div>
        </div>
      </div>
      <!-- A4 42 (Recommended) -->
      <div class="paper-card active" id="card-a4-max" onclick="setPaper('a4-max')">
        <div class="radio-circle"><div class="radio-circle-dot"></div></div>
        <div class="paper-info">
          <div class="title">A4 Paper <span class="title-highlight">(42 photos)</span></div>
          <div class="desc">Max Yield</div>
        </div>
        <div class="badge-recommended">&#10022; Recommended</div>
      </div>
    </div>
  </div>

  <!-- Step 2: Photos & Copy Counts -->
  <div class="section-card">
    <div class="card-header">
      <div class="card-title-left">
        <div class="step-badge">2</div>
        <div class="step-icon">
          <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
        </div>
        <div class="card-title">Photos &amp; Copy Counts</div>
      </div>
      <div class="action-buttons">
        <button class="btn-outline" onclick="triggerFilePicker()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Add Photo(s)
        </button>
        <button class="btn-outline" onclick="triggerFolderPicker()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          + Add Folder
        </button>
        <button class="btn-outline" onclick="fillEvenly()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
          Fill Sheet Evenly
        </button>
      </div>
    </div>

    <!-- Table Wrap for responsive overflow -->
    <div class="photo-table-wrap">
      <div class="photo-table">
        <div class="table-header">
          <div class="th-check">
            <div class="checkbox-custom checked" id="check-all" onclick="toggleSelectAll()">
              <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
          </div>
          <div class="th-name">File Name</div>
          <div class="th-copies">Copies</div>
          <div class="th-action">
            <button class="btn-clear-all" onclick="clearAll()">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              Clear All
            </button>
          </div>
        </div>

        <div id="photo-rows-container">
          <!-- Rendered dynamically -->
        </div>
      </div>
    </div>

    <!-- Table Footer Allocation Bar -->
    <div class="card-footer">
      <div class="pill-allocated" id="allocation-badge">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
        <span id="allocation-text">Allocated: 0 / 42 slots</span>
      </div>
      <div class="toggle-wrap" onclick="toggleAutoFill()">
        <div class="toggle-switch on" id="toggle-autofill">
          <div class="toggle-thumb"></div>
        </div>
        <div class="toggle-label">Auto-fill free slots (repeat photos to avoid empty paper) &#x24D8;</div>
      </div>
    </div>
  </div>

  <!-- Step 3: Background & Border -->
  <div class="section-card">
    <div class="card-header">
      <div class="card-title-left">
        <div class="step-badge">3</div>
        <div class="step-icon">
          <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
        </div>
        <div class="card-title">Background &amp; Border</div>
      </div>
    </div>

    <div class="bg-options-row">
      <div class="bg-left">
        <div class="bg-title">Background:</div>
        <div class="color-pills">
          <div class="pill-color active" id="bg-white" onclick="setBg('#FFFFFF', 'bg-white')">
            <div class="pill-radio"><div class="pill-radio-dot"></div></div>
            White
          </div>
          <div class="pill-color" id="bg-lightblue" onclick="setBg('#E0F2FE', 'bg-lightblue')">
            <div class="pill-radio"><div class="pill-radio-dot"></div></div>
            Light Blue
          </div>
          <div class="pill-color" id="bg-offwhite" onclick="setBg('#F8F9FA', 'bg-offwhite')">
            <div class="pill-radio"><div class="pill-radio-dot"></div></div>
            Off-White
          </div>
          <div class="pill-color" id="bg-custom" onclick="pickCustomColor()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            Custom...
          </div>
        </div>

        <!-- Cutting Borders + Pixel Width Stepper Setting -->
        <div class="guides-setting-wrap">
          <div class="guides-checkbox-wrap" onclick="toggleGuides()">
            <div class="checkbox-custom checked" id="check-guides">
              <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <div class="guides-label">Draw black cutting borders</div>
          </div>
          <div class="border-width-picker" id="border-picker-wrap">
            <span class="border-picker-label">Width:</span>
            <div class="border-stepper">
              <button class="border-step-btn" onclick="changeBorderWidth(-1)">&minus;</button>
              <div class="border-step-val" id="border-width-val">2px</div>
              <button class="border-step-btn" onclick="changeBorderWidth(1)">+</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Diagram on right -->
      <div class="preview-diagram">
        <img src="/api/first_thumb" id="diag-thumb" class="diag-photo" alt="Photo" onerror="this.src='/api/blank_thumb'">
        <div class="diag-arrow">
          <svg viewBox="0 0 24 16"><path d="M2 14c4-12 12-12 18-4m0 0l-4-1m4 1l-2 4"/></svg>
        </div>
        <div class="diag-sheet" id="diag-sheet-box">
          <div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div>
          <div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div>
          <div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div><div class="diag-cell"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Big Primary Action Button -->
  <button class="btn-generate" onclick="generateSheet()">
    <svg viewBox="0 0 24 24"><path d="M19 8H5a3 3 0 0 0-3 3v6h4v4h12v-4h4v-6a3 3 0 0 0-3-3zm-3 11H8v-5h8v5zm3-7a1 1 0 1 1 0-2 1 1 0 0 1 0 2zM18 3H6v3h12V3z"/></svg>
    Generate Print-Ready Passport Sheet
  </button>

  <!-- Print Instructions Card -->
  <div class="footer-card">
    <div class="footer-left">
      <div class="footer-icon">
        <svg viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg>
      </div>
      <div>
        <div class="footer-title">Print Instructions</div>
        <div class="footer-bullets">
          &bull; Paper Size: Match your selection (4&times;6&rdquo; or A4 paper).<br>
          &bull; Scale Setting: ALWAYS select &ldquo;Actual size&rdquo; or &ldquo;100%&rdquo; in print dialog (never &ldquo;Fit to page&rdquo;).
        </div>
      </div>
    </div>
    <div class="footer-actions">
      <button class="btn-outline" onclick="openFolder()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        Open Folder
      </button>
      <button class="btn-outline" onclick="viewSheet()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        View Sheet
      </button>
    </div>
  </div>

</div>

<div class="toast" id="toast">Ready</div>

<script>
let state = null;
let dragCounter = 0;
const validExtensions = ['png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff', 'tif'];

// Timestamp of last explicit API action – used to debounce focus/visibility refreshes
let lastApiCallTime = 0;

function markApiCall() { lastApiCallTime = Date.now(); }

function showToast(msg) {
  const t = document.getElementById('toast');
  t.innerText = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = 'none'; }, 3500);
}

// -------------------------------------------------------------
// Native File & Folder Pickers (Browser-Native = 100% Reliable)
// -------------------------------------------------------------
const fileInput = document.getElementById('native-file-picker');
const folderInput = document.getElementById('native-folder-picker');

function triggerFilePicker() {
  // Reset value first so the change event fires even if the same file is selected again
  fileInput.value = '';
  fileInput.click();
}

function triggerFolderPicker() {
  folderInput.value = '';
  folderInput.click();
}

fileInput.addEventListener('change', async (e) => {
  if (e.target.files && e.target.files.length > 0) {
    await uploadFileList(Array.from(e.target.files));
    e.target.value = '';
  }
});

folderInput.addEventListener('change', async (e) => {
  if (e.target.files && e.target.files.length > 0) {
    await uploadFileList(Array.from(e.target.files));
    e.target.value = '';
  }
});

// -------------------------------------------------------------
// Fast Binary Upload Stream
// -------------------------------------------------------------
async function uploadFileList(files) {
  const imageFiles = files.filter(f => {
    const ext = f.name.split('.').pop().toLowerCase();
    return validExtensions.includes(ext);
  });

  if (imageFiles.length === 0) {
    showToast("No supported image files found (.jpg, .png, .webp, etc.)");
    return;
  }

  showToast(`Uploading ${imageFiles.length} photo(s)...`);
  let added = 0;

  for (let i = 0; i < imageFiles.length; i++) {
    const f = imageFiles[i];
    try {
      const url = '/api/upload_raw?name=' + encodeURIComponent(f.name);
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: f
      });
      if (!res.ok) continue;
      const data = await res.json();
      if (data.ok) added++;
    } catch (err) {
      console.error("Upload error for file:", f.name, err);
    }
  }

  showToast(`Added ${added} photo(s) to queue!`);
  markApiCall();
  await fetchState();
}

// -------------------------------------------------------------
// Drag & Drop (fixed: loops readEntries until exhausted)
// -------------------------------------------------------------
const overlay = document.getElementById('drag-overlay');

window.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragCounter++;
  overlay.classList.add('active');
});

window.addEventListener('dragleave', (e) => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter <= 0) {
    dragCounter = 0;
    overlay.classList.remove('active');
  }
});

window.addEventListener('dragover', (e) => {
  e.preventDefault();
});

window.addEventListener('drop', async (e) => {
  e.preventDefault();
  dragCounter = 0;
  overlay.classList.remove('active');

  const dt = e.dataTransfer;
  let collectedFiles = [];

  // FIX: readEntries() must be called in a loop until it returns [] (WebKit API contract)
  async function readAllFromReader(dirReader) {
    let allEntries = [];
    while (true) {
      const batch = await new Promise((resolve) => {
        dirReader.readEntries(
          (results) => resolve(results),
          () => resolve([])
        );
      });
      if (!batch || batch.length === 0) break;
      allEntries = allEntries.concat(batch);
    }
    return allEntries;
  }

  async function readEntry(entry) {
    if (entry.isFile) {
      return new Promise((resolve) => {
        entry.file(
          (file) => resolve([file]),
          () => resolve([])
        );
      });
    } else if (entry.isDirectory) {
      const dirReader = entry.createReader();
      const entries = await readAllFromReader(dirReader);
      const nested = await Promise.all(entries.map(readEntry));
      return nested.flat();
    }
    return [];
  }

  if (dt.items && dt.items.length > 0 && typeof dt.items[0].webkitGetAsEntry === 'function') {
    try {
      const entryPromises = [];
      for (let i = 0; i < dt.items.length; i++) {
        const entry = dt.items[i].webkitGetAsEntry();
        if (entry) entryPromises.push(readEntry(entry));
      }
      const allLists = await Promise.all(entryPromises);
      collectedFiles = allLists.flat();
    } catch (err) {
      console.error("Folder entry error, falling back to dt.files:", err);
      collectedFiles = Array.from(dt.files);
    }
  } else {
    collectedFiles = Array.from(dt.files);
  }

  // Final safety fallback
  if (collectedFiles.length === 0 && dt.files && dt.files.length > 0) {
    collectedFiles = Array.from(dt.files);
  }

  if (collectedFiles.length > 0) {
    await uploadFileList(collectedFiles);
  } else {
    showToast("No supported image files found in the dropped items.");
  }
});

// -------------------------------------------------------------
// State Management & Rendering
// -------------------------------------------------------------
async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    state = await res.json();
    renderState();
  } catch (e) {
    console.error("fetchState error:", e);
  }
}

function renderState() {
  if (!state) return;

  // Paper selection
  ['4x6', 'a4', 'a4-max'].forEach(key => {
    const card = document.getElementById('card-' + key);
    if (card) {
      if (state.paper === key) card.classList.add('active');
      else card.classList.remove('active');
    }
  });

  // Background pills
  document.querySelectorAll('.pill-color').forEach(p => p.classList.remove('active'));
  if (state.bg === '#FFFFFF') document.getElementById('bg-white')?.classList.add('active');
  else if (state.bg === '#E0F2FE') document.getElementById('bg-lightblue')?.classList.add('active');
  else if (state.bg === '#F8F9FA') document.getElementById('bg-offwhite')?.classList.add('active');
  else document.getElementById('bg-custom')?.classList.add('active');

  // Guides & Border Width
  const guidesBox = document.getElementById('check-guides');
  if (state.guides) guidesBox.classList.add('checked');
  else guidesBox.classList.remove('checked');

  const borderVal = document.getElementById('border-width-val');
  if (borderVal) borderVal.innerText = (state.border_width || 2) + 'px';

  const diagSheet = document.getElementById('diag-sheet-box');
  if (diagSheet) {
    diagSheet.style.borderWidth = state.guides ? (state.border_width || 2) + 'px' : '0px';
  }

  // Auto-fill toggle
  const toggle = document.getElementById('toggle-autofill');
  if (state.auto_fill) toggle.classList.add('on');
  else toggle.classList.remove('on');

  // Allocation label
  const allocText = document.getElementById('allocation-text');
  if (state.empty > 0) {
    allocText.innerText = `Allocated: ${state.allocated} / ${state.capacity} slots (${state.empty} empty slots)`;
  } else if (state.allocated === state.capacity) {
    allocText.innerText = `Allocated: ${state.allocated} / ${state.capacity} slots (Full — 100% Fit)`;
  } else {
    allocText.innerText = `Allocated: ${state.allocated} copies (Will create multiple sheets)`;
  }

  // Render photo rows
  const container = document.getElementById('photo-rows-container');
  container.innerHTML = '';

  if (!state.photos || state.photos.length === 0) {
    container.innerHTML = `
      <div class="empty-drop-zone" onclick="triggerFilePicker()">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#94A3B8" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <p>No photos in queue</p>
        <span>Drag and drop photos or folders here, or click to browse</span>
      </div>
    `;
  } else {
    const allSelected = state.photos.every(p => p.selected);
    const checkAll = document.getElementById('check-all');
    if (allSelected) checkAll.classList.add('checked');
    else checkAll.classList.remove('checked');

    state.photos.forEach(photo => {
      const row = document.createElement('div');
      row.className = 'photo-row';
      row.innerHTML = `
        <div class="td-check">
          <div class="checkbox-custom ${photo.selected ? 'checked' : ''}" onclick="togglePhoto(${photo.id})">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        </div>
        <img src="/api/thumb?id=${photo.id}" class="td-thumb" alt="${photo.name}" onerror="this.src='/api/blank_thumb'">
        <div class="td-info">
          <div class="td-name">${photo.name}</div>
          <div class="td-meta">${photo.meta_str}</div>
        </div>
        <div class="td-copies">
          <span class="copies-label">Copies:</span>
          <div class="stepper">
            <button class="stepper-btn" onclick="changeCopies(${photo.id}, -1)">&minus;</button>
            <div class="stepper-val">${photo.count}</div>
            <button class="stepper-btn" onclick="changeCopies(${photo.id}, 1)">+</button>
          </div>
        </div>
        <div class="td-action">
          <button class="btn-remove" onclick="removePhoto(${photo.id})">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            Remove
          </button>
        </div>
      `;
      container.appendChild(row);
    });
  }

  // Update mini diagram photo
  const diagThumb = document.getElementById('diag-thumb');
  if (diagThumb) {
    diagThumb.src = '/api/first_thumb?t=' + Date.now();
  }
}

async function setPaper(paperKey) {
  markApiCall();
  await fetch('/api/set_paper', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paper: paperKey}) });
  fetchState();
}

async function setBg(hex, elId) {
  markApiCall();
  await fetch('/api/set_bg', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bg: hex}) });
  fetchState();
}

async function pickCustomColor() {
  markApiCall();
  const res = await fetch('/api/pick_color', { method: 'POST' });
  const data = await res.json();
  if (data.bg) fetchState();
}

async function toggleGuides() {
  markApiCall();
  await fetch('/api/toggle_guides', { method: 'POST' });
  fetchState();
}

async function changeBorderWidth(delta) {
  if (!state) return;
  const current = state.border_width || 2;
  // FIX: Allow full range 1-10px (UI was previously capped at 6)
  const newWidth = Math.max(1, Math.min(10, current + delta));
  markApiCall();
  await fetch('/api/set_border_width', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ width: newWidth })
  });
  showToast(`Border width set to ${newWidth}px`);
  fetchState();
}

async function toggleAutoFill() {
  markApiCall();
  await fetch('/api/toggle_autofill', { method: 'POST' });
  fetchState();
}

async function changeCopies(id, delta) {
  markApiCall();
  await fetch('/api/change_copies', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, delta}) });
  fetchState();
}

async function togglePhoto(id) {
  markApiCall();
  await fetch('/api/toggle_photo', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) });
  fetchState();
}

async function toggleSelectAll() {
  markApiCall();
  await fetch('/api/toggle_all', { method: 'POST' });
  fetchState();
}

async function removePhoto(id) {
  markApiCall();
  await fetch('/api/remove_photo', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) });
  fetchState();
}

async function clearAll() {
  markApiCall();
  await fetch('/api/clear_all', { method: 'POST' });
  fetchState();
}

async function fillEvenly() {
  markApiCall();
  const res = await fetch('/api/fill_evenly', { method: 'POST' });
  const data = await res.json();
  if (!data.ok && data.msg) {
    showToast(data.msg);
    return;
  }
  fetchState();
}

async function generateSheet() {
  if (!state || !state.photos || state.photos.length === 0) {
    showToast("Please add at least one photo first!");
    return;
  }
  const hasSelected = state.photos.some(p => p.selected);
  if (!hasSelected) {
    showToast("No photos selected! Please check at least one photo.");
    return;
  }

  // Show spinner while generating (generation can take a few seconds)
  const spinner = document.getElementById('spinner-overlay');
  spinner.classList.add('active');
  markApiCall();

  try {
    const res = await fetch('/api/generate', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`Created ${data.count} sheet(s)! Opening preview...`);
      // FIX: refresh state so thumbnail and allocation reflect the new sheet
      await fetchState();
    } else {
      alert("Error: " + (data.error || "Unknown error"));
    }
  } catch (err) {
    alert("Network error while generating: " + err);
  } finally {
    spinner.classList.remove('active');
  }
}

async function openFolder() {
  await fetch('/api/open_folder', { method: 'POST' });
}

async function viewSheet() {
  const res = await fetch('/api/view_sheet', { method: 'POST' });
  const data = await res.json();
  if (!data.ok && data.msg) {
    showToast(data.msg);
  }
}  // FIX: This closing brace was missing — caused all global JS below to be trapped inside this function

// FIX: Debounce focus/visibility refresh so it doesn't overwrite a just-sent toggle (300 ms guard)
window.addEventListener('focus', () => {
  if (Date.now() - lastApiCallTime > 300) fetchState();
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && Date.now() - lastApiCallTime > 300) fetchState();
});

// Notify server to shut down cleanly when user closes the window
window.addEventListener('beforeunload', () => {
  navigator.sendBeacon('/api/exit');
});

// Initialize
fetchState();
</script>

</body>
</html>
"""


class PassportHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        with STATE_LOCK:
            APP_STATE["last_ping"] = time.time()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return

        elif path == "/api/state":
            alloc = calculate_allocation()
            with STATE_LOCK:
                resp = {
                    "paper": APP_STATE["paper"],
                    "bg": APP_STATE["bg"],
                    "guides": APP_STATE["guides"],
                    "border_width": APP_STATE["border_width"],
                    "auto_fill": APP_STATE["auto_fill"],
                    "photos": APP_STATE["photos"],
                    "capacity": alloc["capacity"],
                    "allocated": alloc["allocated"],
                    "empty": alloc["empty"]
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        elif path == "/api/thumb":
            photo_id = int(query.get("id", ["0"])[0])
            with STATE_LOCK:
                photo_obj = next((p for p in APP_STATE["photos"] if p["id"] == photo_id), None)
            if photo_obj:
                try:
                    p = Path(photo_obj["path"])
                    if p.is_file():
                        with STATE_LOCK:
                            bg_rgb = ImageColor.getrgb(APP_STATE["bg"])
                        base_img = load_and_composite(p, bg_rgb)
                        thumb = crop_and_resize(base_img, p.name, target_w=88, target_h=112)
                        buf = io.BytesIO()
                        thumb.save(buf, format="JPEG", quality=90)
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        self.wfile.write(buf.getvalue())
                        return
                except Exception:
                    pass
            self._send_blank_thumb()
            return

        elif path == "/api/first_thumb":
            with STATE_LOCK:
                photo_obj = APP_STATE["photos"][0] if APP_STATE["photos"] else None
            if photo_obj:
                try:
                    p = Path(photo_obj["path"])
                    if p.is_file():
                        with STATE_LOCK:
                            bg_rgb = ImageColor.getrgb(APP_STATE["bg"])
                        base_img = load_and_composite(p, bg_rgb)
                        thumb = crop_and_resize(base_img, p.name, target_w=64, target_h=84)
                        buf = io.BytesIO()
                        thumb.save(buf, format="JPEG", quality=90)
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        self.wfile.write(buf.getvalue())
                        return
                except Exception:
                    pass
            self._send_blank_thumb()
            return

        elif path == "/api/blank_thumb":
            self._send_blank_thumb()
            return

        elif path == "/api/heartbeat":
            self.send_response(200)
            self.end_headers()
            return

        self.send_error(404)

    def _send_blank_thumb(self):
        img = Image.new("RGB", (64, 84), (240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(buf.getvalue())

    def do_POST(self):
        with STATE_LOCK:
            APP_STATE["last_ping"] = time.time()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # -------------------------------------------------------------
        # High-Speed Raw Binary File Upload Endpoint
        # -------------------------------------------------------------
        if path == "/api/upload_raw":
            query = urllib.parse.parse_qs(parsed.query)
            filename = query.get("name", ["photo.jpg"])[0]
            filename = Path(filename).name  # Safe basename

            upload_dir = Path.cwd() / "PHOTOS"
            upload_dir.mkdir(parents=True, exist_ok=True)
            save_path = upload_dir / filename

            content_len = int(self.headers.get("Content-Length", 0))
            with open(save_path, "wb") as f:
                remaining = content_len
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            # Validate image
            try:
                with Image.open(save_path) as img:
                    img.verify()
            except Exception:
                save_path.unlink(missing_ok=True)
                self._send_json({"ok": False, "error": "Invalid image format"})
                return

            with STATE_LOCK:
                resolved = str(save_path.resolve())
                existing_item = next((p for p in APP_STATE["photos"] if p["path"] == resolved), None)
                if existing_item:
                    # Already in queue — increment count instead of adding a duplicate
                    existing_item["count"] = existing_item["count"] + 1
                    existing_item["selected"] = True
                else:
                    meta = get_file_metadata(save_path)
                    next_id = max([p["id"] for p in APP_STATE["photos"]] + [-1]) + 1
                    APP_STATE["photos"].append({
                        "id": next_id,
                        "path": resolved,
                        "name": save_path.name,
                        "selected": True,
                        "count": 1,   # FIX: default 1 copy (was 3)
                        "meta_str": meta["meta_str"]
                    })

            self._send_json({"ok": True, "name": save_path.name})
            return

        # Handle other JSON endpoints
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            req_data = json.loads(body)
        except Exception:
            req_data = {}

        if path == "/api/set_paper":
            with STATE_LOCK:
                if req_data.get("paper") in PAPER_CONFIGS:
                    APP_STATE["paper"] = req_data["paper"]
            self._send_json({"ok": True})

        elif path == "/api/set_bg":
            with STATE_LOCK:
                APP_STATE["bg"] = req_data.get("bg", "#FFFFFF")
            self._send_json({"ok": True})

        elif path == "/api/pick_color":
            color = self._run_color_dialog()
            if color:
                with STATE_LOCK:
                    APP_STATE["bg"] = color.upper()
                self._send_json({"bg": color.upper()})
            else:
                self._send_json({"bg": None})

        elif path == "/api/toggle_guides":
            with STATE_LOCK:
                APP_STATE["guides"] = not APP_STATE["guides"]
            self._send_json({"ok": True})

        elif path == "/api/set_border_width":
            with STATE_LOCK:
                val = int(req_data.get("width", 2))
                APP_STATE["border_width"] = max(1, min(10, val))
            self._send_json({"ok": True, "width": APP_STATE["border_width"]})

        elif path == "/api/toggle_autofill":
            with STATE_LOCK:
                APP_STATE["auto_fill"] = not APP_STATE["auto_fill"]
            self._send_json({"ok": True})

        elif path == "/api/change_copies":
            photo_id = req_data.get("id")
            delta = req_data.get("delta", 0)
            with STATE_LOCK:
                for p in APP_STATE["photos"]:
                    if p["id"] == photo_id:
                        p["count"] = max(1, p["count"] + delta)
            self._send_json({"ok": True})

        elif path == "/api/toggle_photo":
            photo_id = req_data.get("id")
            with STATE_LOCK:
                for p in APP_STATE["photos"]:
                    if p["id"] == photo_id:
                        p["selected"] = not p["selected"]
            self._send_json({"ok": True})

        elif path == "/api/toggle_all":
            with STATE_LOCK:
                all_sel = all(p["selected"] for p in APP_STATE["photos"])
                for p in APP_STATE["photos"]:
                    p["selected"] = not all_sel
            self._send_json({"ok": True})

        elif path == "/api/remove_photo":
            photo_id = req_data.get("id")
            with STATE_LOCK:
                APP_STATE["photos"] = [p for p in APP_STATE["photos"] if p["id"] != photo_id]
            self._send_json({"ok": True})

        elif path == "/api/clear_all":
            with STATE_LOCK:
                APP_STATE["photos"].clear()
            self._send_json({"ok": True})

        elif path == "/api/fill_evenly":
            # FIX: Guard against division-by-zero when no photos are selected
            with STATE_LOCK:
                cap = PAPER_CONFIGS[APP_STATE["paper"]]["capacity"]
                active = [p for p in APP_STATE["photos"] if p["selected"]]
                if not active:
                    self._send_json({"ok": False, "msg": "No photos selected to fill evenly."})
                    return
                n = len(active)
                base = cap // n
                rem = cap % n
                for i, p in enumerate(active):
                    p["count"] = max(1, base + (1 if i < rem else 0))
            self._send_json({"ok": True})

        elif path == "/api/generate":
            # FIX: Snapshot mutable state under lock, then release lock before heavy image processing.
            # Previously the lock was held during the entire generation loop, blocking all other
            # API endpoints (upload, setPaper, etc.) and making the UI appear frozen.
            try:
                with STATE_LOCK:
                    paper_key = APP_STATE["paper"]
                    cfg = PAPER_CONFIGS[paper_key]
                    cap = cfg["capacity"]
                    bg_hex = APP_STATE["bg"]
                    guides = APP_STATE["guides"]
                    border_w = APP_STATE.get("border_width", 2)
                    auto_fill = APP_STATE["auto_fill"]
                    # Deep-copy selected items (path, count) so the lock can be released
                    selected = [
                        {"path": p["path"], "count": p["count"]}
                        for p in APP_STATE["photos"] if p["selected"]
                    ]

                # --- Lock released here; UI remains responsive during sheet generation ---

                if not selected:
                    self._send_json({"success": False, "error": "No photos selected!"})
                    return

                bg_rgb = ImageColor.getrgb(bg_hex)

                import itertools
                expanded = []
                for item in selected:
                    p = Path(item["path"])
                    if not p.is_file():
                        continue
                    base_img = load_and_composite(p, bg_rgb)
                    passport_photo = crop_and_resize(base_img, p.name)
                    expanded.extend([passport_photo] * item["count"])

                if not expanded:
                    self._send_json({"success": False, "error": "Selected image files could not be found."})
                    return

                out_dir = Path.cwd() / "Passport_Sheets_Output"
                out_dir.mkdir(parents=True, exist_ok=True)

                num_sheets = (len(expanded) + cap - 1) // cap if len(expanded) > 0 else 1
                generated_files = []

                for s_idx in range(num_sheets):
                    chunk = expanded[s_idx * cap : (s_idx + 1) * cap]
                    if auto_fill and len(chunk) < cap and expanded:
                        cycled = itertools.cycle(expanded)
                        chunk = [next(cycled) for _ in range(cap)]

                    sheet = build_passport_sheet(
                        chunk,
                        bg_rgb,
                        draw_guides=guides,
                        border_width=border_w,
                        paper_key=paper_key
                    )
                    suffix = f"_{s_idx + 1}" if num_sheets > 1 else ""
                    out_file = out_dir / f"passport_{paper_key}{suffix}.png"
                    sheet.save(str(out_file), format="PNG", dpi=(300, 300))
                    generated_files.append(out_file)

                # Write results back under lock
                with STATE_LOCK:
                    APP_STATE["latest_file"] = generated_files[0] if generated_files else None
                    APP_STATE["latest_dir"] = out_dir

                # Open preview
                if generated_files:
                    try:
                        os.startfile(str(generated_files[0]))
                    except Exception:
                        pass

                self._send_json({"success": True, "count": len(generated_files)})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)})

        elif path == "/api/view_sheet":
            with STATE_LOCK:
                f = APP_STATE["latest_file"]
            if f and Path(f).exists():
                os.startfile(str(f))
                self._send_json({"ok": True})
            else:
                out_dir = Path.cwd() / "Passport_Sheets_Output"
                if out_dir.exists() and any(out_dir.glob("*.png")):
                    first_png = next(out_dir.glob("*.png"))
                    os.startfile(str(first_png))
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "msg": "No sheet generated yet. Click 'Generate' first!"})

        elif path == "/api/open_folder":
            out_dir = Path.cwd() / "Passport_Sheets_Output"
            out_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(out_dir))
            self._send_json({"ok": True})

        elif path == "/api/exit":
            self._send_json({"ok": True})
            def delayed_exit():
                time.sleep(0.3)
                # FIX: Graceful shutdown — release socket before exiting
                try:
                    _server_ref.shutdown()
                    _server_ref.server_close()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=delayed_exit, daemon=True).start()

        else:
            self.send_error(404)

    def _run_color_dialog(self):
        """Runs color picker in a dedicated clean subprocess to avoid thread deadlocks."""
        try:
            with STATE_LOCK:
                curr = APP_STATE["bg"]
            script = f"""
import tkinter as tk
from tkinter import colorchooser
r = tk.Tk()
r.withdraw()
r.attributes('-topmost', True)
c = colorchooser.askcolor(title='Pick Background Color', initialcolor='{curr}')
r.destroy()
if c and c[1]:
    print(c[1])
"""
            res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            val = res.stdout.strip()
            return val if val.startswith("#") else None
        except Exception:
            return None

    def _send_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_edge_path():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# Global reference to server so the /api/exit handler can shut it down gracefully
_server_ref = None


def run_app():
    global _server_ref

    init_photos_from_folder()

    port = get_free_port()
    server = ThreadingHTTPServer(('127.0.0.1', port), PassportHTTPHandler)
    server.daemon_threads = True
    _server_ref = server

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{port}"
    edge = find_edge_path()

    print(f"Passport Photo Sheet Maker running at: {url}")

    if edge:
        profile_dir = Path.home() / ".passport_sheet_maker_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            edge,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1180,880",
            "--window-position=120,60",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        edge_proc = subprocess.Popen(cmd)
        try:
            edge_proc.wait()
        except KeyboardInterrupt:
            edge_proc.terminate()
    else:
        import webbrowser
        webbrowser.open(url)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass

    # FIX: Graceful shutdown — release socket so port is free on next launch
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass


if __name__ == "__main__":
    run_app()
