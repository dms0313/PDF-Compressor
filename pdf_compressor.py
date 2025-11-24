import os
import io
import uuid
import threading
import tempfile
import logging
import re
import time
import subprocess
import shutil
from typing import Optional, Dict, Any, List, Set

from flask import Flask, request, jsonify, send_file, render_template_string, abort
import fitz  # PyMuPDF
from PIL import Image, ImageOps
import pikepdf

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pdf-compressor")

# -----------------------------
# Flask app
# -----------------------------
app = Flask(__name__)
# 600 MB hard cap for uploads (server-side). Frontend also checks.
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024

# In-memory job store
# job_id -> { status, progress, output_buffer, error, created_at, input_path }
jobs: Dict[str, Dict[str, Any]] = {}

# -----------------------------
# Frontend (unchanged)
# -----------------------------

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PDF Compressor for Architectural Drawings</title>
  <style>
    body { background: #1e1e1e; color: #cfcfcf; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; }
    .container { max-width: 800px; margin: 0 auto; }
    h1 { text-align: center; color: #4caf50; margin-bottom: 30px; }
    .info { background: #2a2a2a; border-radius: 8px; padding: 15px; margin-bottom: 20px; font-size: 0.9rem; }
    #drop_zone { background: #2e2e2e; border: 3px dashed #555; border-radius: 12px; width: 100%; height: 200px; display: flex; align-items: center; justify-content: center; font-size: 1.2rem; transition: all 0.3s ease; cursor: pointer; margin-bottom: 20px; }
    #drop_zone.hover { background: #3e3e3e; border-color: #4caf50; }
    #drop_zone:hover { background: #353535; }
    #file_input { display: none; }
    #progress_container { width: 100%; height: 25px; background: #444; border-radius: 12px; overflow: hidden; display: none; margin-bottom: 10px; }
    #progress_bar { height: 100%; width: 0%; background: linear-gradient(90deg, #4caf50, #45a049); transition: width 0.3s ease; }
    #status { text-align: center; margin-top: 10px; font-size: 1rem; min-height: 1.5rem; font-weight: 500; }
    .settings, .page-filter { background: #2a2a2a; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
    .setting-group { margin-bottom: 15px; }
    label { display: block; margin-bottom: 5px; font-weight: 500; }
    select, input[type="range"], input[type="checkbox"] { width: 100%; padding: 8px; background: #3a3a3a; border: 1px solid #555; border-radius: 4px; color: #cfcfcf; box-sizing: border-box; }
    input[type="range"] { padding: 0; height: 25px; }
    input[type="checkbox"] { width: auto; vertical-align: middle; }
    .range-value { display: inline-block; margin-left: 10px; font-weight: bold; color: #4caf50; }
    .filter-buttons { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 10px; }
    .filter-btn { background: #3a3a3a; border: 2px solid #555; color: #cfcfcf; padding: 10px 15px; border-radius: 6px; cursor: pointer; transition: all 0.3s ease; font-size: 0.9rem; text-align: center; }
    .filter-btn:hover { background: #4a4a4a; }
    .filter-btn.active { background: #4caf50; border-color: #4caf50; color: white; }
    .filter-btn.active:hover { background: #45a049; }
    .action-btn { background: #2196f3; border: none; color: white; padding: 12px 24px; border-radius: 6px; cursor: pointer; font-size: 1rem; width: 100%; margin-top: 10px; transition: background 0.3s ease; }
    .action-btn:hover { background: #1976d2; }
    .action-btn:disabled { background: #555; cursor: not-allowed; }
    #compress_btn { background: #4caf50; }
    #compress_btn:hover { background: #45a049; }
    .page-info { background: #333; border-radius: 4px; padding: 10px; margin-top: 15px; font-size: 0.85rem; max-height: 200px; overflow-y: auto; }
    .page-summary { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid #444; }
    .page-summary:last-child { border-bottom: none; }
  </style>
</head>
<body>
  <div class="container">
    <h1>📐 PDF Extractor & Compressor for Architectural Drawings</h1>
    <div class="info">
      <strong>Optimized for:</strong> CAD drawings, blueprints, floor plans, and technical diagrams.<br>
      <strong>Supported formats:</strong> PDF files up to 100MB
    </div>

    <div id="drop_zone">Drop PDF here or click to browse</div>
    <input type="file" id="file_input" accept=".pdf" multiple>

    <div class="settings">
      <h3>Compression Settings</h3>
      <div class="setting-group">
        <label for="quality">Image Quality (for color/grayscale images):</label>
        <input type="range" id="quality" min="20" max="95" value="60">
        <span class="range-value" id="quality-value">60</span>
      </div>
      <div class="setting-group">
        <label for="max_dimension">Max Image Dimension (pixels):</label>
        <select id="max_dimension">
          <option value="1200">1200px (High Quality)</option>
          <option value="1000" selected>1000px (Balanced)</option>
          <option value="800">800px (Smaller Size)</option>
        </select>
      </div>
      <div class="setting-group">
        <label for="drawing_mode">Drawing Type:</label>
        <select id="drawing_mode">
          <option value="general" selected>General Architectural</option>
          <option value="line_art">Line Art/CAD</option>
          <option value="mixed">Mixed Content</option>
        </select>
      </div>
      <div class="setting-group">
          <label for="extreme_compression" style="display:inline-block; vertical-align: middle;">Extreme Compression:</label>
          <input type="checkbox" id="extreme_compression">
          <span style="font-size: 0.8rem; vertical-align: middle; margin-left: 5px;">(Forces 1-bit B&W images and 72 DPI. Best for size.)</span>
      </div>
    </div>

    <div class="page-filter">
      <h3>📄 Page Filtering (Optional)</h3>
      <p style="font-size: 0.9rem; margin-bottom: 15px;">
        Analyze your PDF to select specific page types to extract. If no pages are selected, all pages will be processed.
      </p>
      <button id="analyze_btn" class="action-btn" disabled>🔍 Analyze PDF Pages</button>
      <div id="page_analysis" style="display: none;">
        <div class="filter-buttons" id="filter_buttons"></div>
        <div id="page_info" class="page-info" style="display: none;"></div>
      </div>
    </div>

    <button id="compress_btn" class="action-btn" disabled>🚀 Extract & Download</button>

    <div id="progress_container">
      <div id="progress_bar"></div>
    </div>
    <div id="status"></div>
  </div>

  <script>
    const dz = document.getElementById('drop_zone'),
          pc = document.getElementById('progress_container'),
          pb = document.getElementById('progress_bar'),
          st = document.getElementById('status'),
          fi = document.getElementById('file_input'),
          qualitySlider = document.getElementById('quality'),
          qualityValue = document.getElementById('quality-value'),
          analyzeBtn = document.getElementById('analyze_btn'),
          compressBtn = document.getElementById('compress_btn'),
          pageAnalysis = document.getElementById('page_analysis'),
          filterButtons = document.getElementById('filter_buttons'),
          pageInfo = document.getElementById('page_info');

    let currentFile = null;
    let fileQueue = [];
    let isProcessingQueue = false;
    let pageAnalysisData = null;
    let pagesToKeep = new Set();

    // Event Listeners
    qualitySlider.addEventListener('input', () => { qualityValue.textContent = qualitySlider.value; });
    analyzeBtn.addEventListener('click', analyzePages);
    compressBtn.addEventListener('click', startCompression);
    dz.addEventListener('click', () => fi.click());
    fi.addEventListener('change', handleFile);
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('hover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('hover'));
    dz.addEventListener('drop', e => {
      e.preventDefault();
      dz.classList.remove('hover');
      if (e.dataTransfer.files && e.dataTransfer.files.length) {
        handleFile({ target: { files: e.dataTransfer.files } });
      }
    });

    function handleFile(e) {
      const files = Array.from(e.target.files || []);
      if (!files.length) return;

      const accepted = [];
      files.forEach(file => {
        if (file.type !== 'application/pdf') {
          alert(`Skipping ${file.name}: Please select a PDF file.`);
          return;
        }
        if (file.size > 500 * 1024 * 1024) { // 500MB limit
          alert(`Skipping ${file.name}: File too large. Maximum size is 500MB.`);
          return;
        }
        fileQueue.push(file);
        accepted.push(file);
      });

      if (!accepted.length) return;

      st.textContent = `${accepted.length} file(s) added to queue. (${fileQueue.length} total)`;
      fi.value = '';

      if (!currentFile && !isProcessingQueue) {
        prepareNextFile();
      }
    }

    function prepareNextFile() {
      currentFile = fileQueue.shift() || null;
      if (!currentFile) {
        dz.textContent = 'Drop PDF here or click to browse';
        compressBtn.disabled = true;
        analyzeBtn.disabled = true;
        return;
      }

      analyzeBtn.disabled = false;
      compressBtn.disabled = false;
      st.textContent = `File selected: ${currentFile.name} (${(currentFile.size/1024/1024).toFixed(1)}MB)`;
      dz.textContent = currentFile.name;

      pageAnalysis.style.display = 'none';
      pageAnalysisData = null;
      pagesToKeep.clear();
      pc.style.display = 'none';
      pb.style.width = '0%';
    }

    function analyzePages() {
      if (!currentFile) return;
      analyzeBtn.disabled = true;
      analyzeBtn.textContent = '🔍 Analyzing...';
      const fd = new FormData();
      fd.append('file', currentFile);
      fetch('/analyze', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(data => {
          if (data.error) throw new Error(data.error);
          pageAnalysisData = data;
          displayAnalysisResults(data);
          pageAnalysis.style.display = 'block';
        })
        .catch(err => { alert('Analysis failed: ' + err.message); })
        .finally(() => {
          analyzeBtn.disabled = false;
          analyzeBtn.textContent = '🔍 Analyze PDF Pages';
        });
    }

    function displayAnalysisResults(data) {
      const sections = data.sections;
      filterButtons.innerHTML = '';
      Object.entries(sections).forEach(([sectionType, pages]) => {
        if (pages.length === 0) return;
        const btn = document.createElement('button');
        btn.className = 'filter-btn';
        btn.innerHTML = getSectionButtonHTML(sectionType, pages);
        btn.onclick = () => toggleSection(sectionType, pages, btn);
        filterButtons.appendChild(btn);
      });
      displayPageDetails(data);
    }
    
    function getSectionButtonHTML(sectionType, pages) {
      const icons = { 'architectural': '🏗️', 'structural': '🏢', 'mechanical': '⚙️', 'electrical': '⚡', 'plumbing': '🚰', 'landscape': '🌿', 'civil': '🛣️', 'fire_safety': '🔥', 'cover_title': '📋', 'details': '🔍', 'schedules': '📊', 'other': '📄' };
      const names = { 'architectural': 'Architectural', 'structural': 'Structural', 'mechanical': 'Mechanical', 'electrical': 'Electrical', 'plumbing': 'Plumbing', 'landscape': 'Landscape', 'civil': 'Civil', 'fire_safety': 'Fire Safety', 'cover_title': 'Cover/Title', 'details': 'Details', 'schedules': 'Schedules', 'other': 'Other' };
      return `${icons[sectionType] || '📄'} Extract ${names[sectionType] || sectionType} (${pages.length} pages)`;
    }

    function toggleSection(sectionType, pages, btn) {
      const isActive = btn.classList.contains('active');
      if (isActive) {
        btn.classList.remove('active');
        pages.forEach(pageNum => pagesToKeep.delete(pageNum));
      } else {
        btn.classList.add('active');
        pages.forEach(pageNum => pagesToKeep.add(pageNum));
      }
      updatePageDetails();
    }

    function displayPageDetails(data) {
      const totalPages = data.total_pages;
      pageInfo.innerHTML = `
        <div style="margin-bottom: 10px;">
          <strong>Total Pages: ${totalPages}</strong> | 
          <strong>Pages to Extract: <span id="extract_count">0</span></strong>
        </div>
        <div id="section_details"></div>`;
      pageInfo.style.display = 'block';
      updatePageDetails();
    }

    function updatePageDetails() {
      if (!pageAnalysisData) return;
      document.getElementById('extract_count').textContent = pagesToKeep.size;
      const sectionDetails = document.getElementById('section_details');
      sectionDetails.innerHTML = '';
      Object.entries(pageAnalysisData.sections).forEach(([sectionType, pages]) => {
        if (pages.length === 0) return;
        const selectedPages = pages.filter(p => pagesToKeep.has(p)).length;
        const div = document.createElement('div');
        div.className = 'page-summary';
        div.innerHTML = `
          <span>${getSectionButtonHTML(sectionType, pages).split(' ')[0]} ${sectionType.replace(/_/g, ' ')}</span>
          <span>
            ${selectedPages > 0 ? `<span style="color: #4caf50;">+${selectedPages}</span>` : `<span>${pages.length}</span>`}
          </span>`;
        sectionDetails.appendChild(div);
      });
    }

    function startCompression() {
      if (!currentFile) {
        prepareNextFile();
      }
      if (!currentFile) return;
      isProcessingQueue = true;
      const fd = new FormData();
      fd.append('file', currentFile);
      fd.append('quality', qualitySlider.value);
      fd.append('max_dimension', document.getElementById('max_dimension').value);
      fd.append('drawing_mode', document.getElementById('drawing_mode').value);
      // NEW: Send extreme compression flag
      fd.append('extreme_compression', document.getElementById('extreme_compression').checked);
      if (pagesToKeep.size > 0) {
        fd.append('extract_pages', Array.from(pagesToKeep).join(','));
      }
      st.textContent = 'Uploading...';
      pb.style.width = '0%';
      pc.style.display = 'block';
      compressBtn.disabled = true;
      analyzeBtn.disabled = true;
      fetch('/compress', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(data => {
          if (data.error) throw new Error(data.error);
          st.textContent = 'Processing...';
          pollJobStatus(data.job_id);
        })
        .catch(err => {
          st.textContent = 'Error: ' + err.message;
          pc.style.display = 'none';
          compressBtn.disabled = false;
          analyzeBtn.disabled = false;
          isProcessingQueue = false;
        });
    }

    function pollJobStatus(jobId) {
      fetch(`/status/${jobId}`)
        .then(r => {
            if (!r.ok) {
                throw new Error(`Server responded with status: ${r.status}`);
            }
            return r.json();
        })
        .then(s => {
          if (!s || s.error) {
            throw new Error(s.error || 'Invalid status response');
          }
          pb.style.width = s.progress + '%';
          st.textContent = s.status;
          if (s.status !== 'done' && s.status !== 'error') {
            setTimeout(() => pollJobStatus(jobId), 1500);
          } else if (s.status === 'done') {
            triggerDownload(jobId);
          } else {
            throw new Error(s.error || 'Job failed on the server.');
          }
        })
        .catch(err => {
          st.textContent = 'Error: ' + err.message;
          pc.style.display = 'none';
          compressBtn.disabled = false;
          analyzeBtn.disabled = false;
          isProcessingQueue = false;
        });
    }

    function triggerDownload(jobId) {
        fetch(`/download/${jobId}`)
            .then(res => {
                if (!res.ok) throw new Error(`Download failed with status: ${res.status}`);
                return res.blob();
            })
            .then(b => {
                const url = URL.createObjectURL(b);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'extracted_' + currentFile.name;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                st.textContent = 'Download complete!';
                compressBtn.disabled = true;
                analyzeBtn.disabled = true;
                setTimeout(() => {
                    pc.style.display = 'none';
                    st.textContent = `File selected: ${currentFile.name}`;
                    moveToNextInQueue();
                }, 1200);
            }).catch(err => {
                st.textContent = 'Download error: ' + err.message;
                compressBtn.disabled = false;
                analyzeBtn.disabled = false;
                isProcessingQueue = false;
            });
    }

    function moveToNextInQueue() {
      currentFile = null;
      if (fileQueue.length) {
        prepareNextFile();
        if (isProcessingQueue) {
          startCompression();
        }
      } else {
        isProcessingQueue = false;
        st.textContent = 'Queue complete! Add more files to process again.';
        dz.textContent = 'Drop PDF here or click to browse';
        compressBtn.disabled = true;
        analyzeBtn.disabled = true;
      }
    }
  </script>
</body>
</html>
"""
# -----------------------------
# Helpers
# -----------------------------
def cleanup_temp_file(filepath: Optional[str]) -> None:
    if not filepath:
        return
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Cleaned up temp file: {filepath}")
    except Exception as e:
        logger.warning(f"Failed to cleanup temp file {filepath}: {e}")

def _find_ghostscript_exe() -> Optional[str]:
    """
    Locate Ghostscript executable across platforms.
    Returns full path or None if not found.
    """
    # Common names
    candidates = ["gs", "gswin64c", "gswin32c"]
    for c in candidates:
        path = shutil.which(c)
        if path:
            return path
    return None

def ghostscript_compress(input_bytes: bytes, extreme: bool = False) -> bytes:
    """
    Re-distill via Ghostscript. If GS not available, returns original bytes.
    'extreme' uses 72 DPI + JBIG2 for mono; 'balanced' uses ~150 DPI.
    """
    gs_exe = _find_ghostscript_exe()
    if not gs_exe:
        logger.warning("Ghostscript not found in PATH; skipping GS compression.")
        return input_bytes

    # Base command
    cmd = [
        gs_exe,
        "-sDEVICE=pdfwrite",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-sOutputFile=%stdout",
        "-dDetectDuplicateImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
    ]

    if extreme:
        logger.info("GS: EXTREME settings (PDF 1.6, /screen, JBIG2, 72dpi).")
        cmd += [
            "-dCompatibilityLevel=1.6",
            "-dPDFSETTINGS=/screen",  # most aggressive
            "-dSubsetFonts=true",
            "-dCompressFonts=true",
            "-dAutoFilterMonoImages=false",
            "-sMonoImageFilter=/JBIG2Encode",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageResolution=72",
            "-dGrayImageResolution=72",
            "-dMonoImageResolution=300",  # for linework clarity; mono at 300 keeps lines crisp
        ]
    else:
        logger.info("GS: BALANCED settings (PDF 1.6, /ebook, ~150dpi).")
        cmd += [
            "-dCompatibilityLevel=1.6",
            "-dPDFSETTINGS=/ebook",
            "-dSubsetFonts=true",
            "-dCompressFonts=true",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageResolution=150",
            "-dGrayImageResolution=150",
            "-dMonoImageResolution=600",  # preserve mono linework
        ]

    # Read from stdin, write to stdout
    try:
        proc = subprocess.Popen(
            cmd + ["-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(input_bytes)
        if proc.returncode != 0:
            logger.error(f"Ghostscript failed (code {proc.returncode}): {err.decode(errors='ignore')}")
            return input_bytes
        logger.info("Ghostscript compression done.")
        return out if out else input_bytes
    except Exception as e:
        logger.error(f"Ghostscript error: {e}")
        return input_bytes

def pikepdf_optimize(input_bytes: bytes) -> bytes:
    """
    QPDF/PikePDF pass to shrink structure, enable Fast Web View, compress streams.
    """
    try:
        with pikepdf.open(io.BytesIO(input_bytes)) as pdf:
            pdf.remove_unreferenced_resources()
            # You can also set encryption or object stream tweaks if needed.
            buf = io.BytesIO()
            pdf.save(
                buf,
                optimize_streams=True,
                compress_streams=True,
                linearize=True,
            )
            logger.info("PikePDF optimize + linearize complete.")
            return buf.getvalue()
    except Exception as e:
        logger.error(f"PikePDF optimize failed: {e}")
        return input_bytes

# -----------------------------
# Analysis & Classification
# -----------------------------
def analyze_pdf_pages(pdf_path: str) -> Optional[Dict[str, Any]]:
    try:
        doc = fitz.open(pdf_path)
        analysis = {
            "total_pages": len(doc),
            "sections": {
                "architectural": [], "structural": [], "mechanical": [], "electrical": [],
                "plumbing": [], "landscape": [], "civil": [], "fire_safety": [],
                "cover_title": [], "details": [], "schedules": [], "other": []
            }
        }
        for page_num, page in enumerate(doc, start=1):
            ptype = classify_page(page)
            analysis["sections"][ptype].append(page_num)
        doc.close()
        return analysis
    except Exception as e:
        logger.error(f"Analyze failed: {e}", exc_info=True)
        return None

def classify_page(page: fitz.Page) -> str:
    """
    Heuristic classifier driven by title-block corner text + whole page text.
    """
    try:
        page_rect = page.rect
        br = fitz.Rect(page_rect.width * 0.7, page_rect.height * 0.7, page_rect.width, page_rect.height)
        corner_text = (page.get_text("text", clip=br, sort=True) or "").upper()
        full_text = (page.get_text("text", sort=True) or "").upper()
    except Exception:
        corner_text, full_text = "", ""

    patterns = {
        "cover_title": r"\b[G][ -.]?\d",
        "architectural": r"\b[A][ -.]?\d",
        "structural": r"\b[S][ -.]?\d",
        "mechanical": r"\b[M][ -.]?\d",
        "electrical": r"\b[E][ -.]?\d",
        "plumbing": r"\b[P][ -.]?\d",
        "landscape": r"\b[L][ -.]?\d",
        "civil": r"\b[C][ -.]?\d",
        "fire_safety": r"\b[F][P]?[ -.]?\d",
    }
    for k, pat in patterns.items():
        if re.search(pat, corner_text):
            return k
    for k, pat in patterns.items():
        if re.search(pat, full_text):
            return k

    discipline_keywords = {
        "fire_safety": ["FIRE PROTECTION", "FIRE ALARM", "SPRINKLER", "EGRESS"],
        "mechanical": ["HVAC", "MECHANICAL", "DUCTWORK"],
        "electrical": ["ELECTRICAL", "LIGHTING", "PANEL", "ONE-LINE"],
        "plumbing": ["PLUMBING", "SANITARY", "STORM DRAIN", "RISER DIAGRAM"],
        "civil": ["CIVIL", "GRADING", "UTILITIES", "SITE PLAN"],
        "architectural": ["FLOOR PLAN", "ELEVATION", "ARCHITECTURAL"],
        "structural": ["STRUCTURAL", "FOUNDATION", "FRAMING"],
        "landscape": ["LANDSCAPE", "PLANTING", "IRRIGATION"],
    }
    for k, words in discipline_keywords.items():
        if any(w in full_text for w in words):
            return k

    general = {
        "cover_title": ["COVER", "SHEET INDEX", "GENERAL NOTES", "LEGEND", "ABBREVIATIONS"],
        "schedules": ["SCHEDULE", "DOOR SCHEDULE", "WINDOW SCHEDULE", "FINISH SCHEDULE"],
        "details": ["DETAIL", "CONNECTION", "ASSEMBLY", "SECTION DETAIL"],
    }
    for k, words in general.items():
        if any(w in full_text for w in words):
            return k

    return "other"

# -----------------------------
# Image recompression
# -----------------------------
def _resample_image(pil_img: Image.Image, max_dim: int, mode: str, quality: int, extreme: bool) -> bytes:
    """
    Convert, resample and encode an image for PDF embedding.
    - For extreme/line art: 1-bit TIFF G4
    - For others: JPEG (RGB/Gray) with given quality
    """
    # Ensure exif transforms applied
    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass

    # Downscale
    if max(pil_img.width, pil_img.height) > max_dim:
        pil_img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    # Choose encoding
    if extreme or mode == "line_art":
        # Ensure bilevel
        img_mono = pil_img.convert("1")
        out = io.BytesIO()
        img_mono.save(out, format="TIFF", compression="group4")
        return out.getvalue()
    else:
        # General / mixed: choose Gray for general, RGB for mixed
        if mode == "general":
            pil_img = pil_img.convert("L")
        else:
            pil_img = pil_img.convert("RGB")
        out = io.BytesIO()
        pil_img.save(out, format="JPEG", optimize=True, quality=max(20, min(95, quality)))
        return out.getvalue()

def _collect_unique_image_xrefs(doc: fitz.Document) -> List[int]:
    xrefs: Set[int] = set()
    for page in doc:
        for info in page.get_images(full=True):
            # info[0] is xref
            xrefs.add(info[0])
    return list(xrefs)

# -----------------------------
# Core processing pipeline
# -----------------------------
def process_job(
    job_id: str,
    input_pdf: str,
    quality: int,
    max_dimension: int,
    drawing_mode: str,
    extract_pages: Optional[List[int]] = None,
    extreme_compression: bool = False,
) -> None:
    try:
        jobs[job_id]["status"] = "Initializing..."
        jobs[job_id]["progress"] = 5

        # 1) Open source and subset pages if requested
        with fitz.open(input_pdf) as src:
            if extract_pages:
                keep = [p - 1 for p in extract_pages]  # incoming pages are 1-based
                keep = [p for p in keep if 0 <= p < src.page_count]
                if not keep:
                    raise ValueError("No valid pages selected.")
                status_label = f"Extracting {len(keep)} selected pages..."
            else:
                keep = list(range(src.page_count))
                status_label = "Processing all pages..."

            jobs[job_id]["status"] = status_label
            jobs[job_id]["progress"] = 10

            with fitz.open() as work:
                for p in keep:
                    try:
                        # Some PDFs contain malformed link destinations like
                        # "1&view=Fit" which PyMuPDF attempts to parse as a
                        # page number and raises ValueError. Fall back to
                        # copying the page without link metadata so the job
                        # can still complete.
                        work.insert_pdf(src, from_page=p, to_page=p)
                    except ValueError as e:
                        logger.warning(
                            "Page %s has invalid link metadata; copying without links (%s)",
                            p + 1,
                            e,
                        )
                        work.insert_pdf(src, from_page=p, to_page=p, links=False)

                # 2) Optional cleanup in extreme mode
                if extreme_compression:
                    jobs[job_id]["status"] = "Cleaning annotations & attachments..."
                    jobs[job_id]["progress"] = 15
                    try:
                        for page in work:
                            ann_iter = page.annots()
                            if ann_iter:
                                to_delete = []
                                for a in ann_iter:
                                    # Broadly remove visual annotations (safer size)
                                    to_delete.append(a)
                                for a in to_delete:
                                    try:
                                        page.delete_annot(a)
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.warning(f"Annot cleanup warning: {e}")

                    # Remove embedded JS files if any
                    try:
                        count = work.embfile_count()
                        for i in range(count - 1, -1, -1):
                            info = work.embfile_info(i)
                            fname = (info.get("filename") or "").lower() if info else ""
                            if fname.endswith(".js"):
                                logger.info(f"Removing embedded JS: {fname}")
                                work.embfile_del(i)
                    except Exception as e:
                        logger.warning(f"Embed cleanup warning: {e}")

                # 3) Image recompression
                jobs[job_id]["status"] = "Recompressing images..."
                jobs[job_id]["progress"] = 20
                xrefs = _collect_unique_image_xrefs(work)
                total = len(xrefs)
                logger.info(f"Found {total} unique images.")
                for idx, xref in enumerate(xrefs, start=1):
                    try:
                        base = work.extract_image(xref)
                        img_bytes = base.get("image")
                        if not img_bytes:
                            continue
                        with Image.open(io.BytesIO(img_bytes)) as im:
                            new_bytes = _resample_image(
                                pil_img=im,
                                max_dim=max_dimension,
                                mode=drawing_mode,
                                quality=quality,
                                extreme=extreme_compression,
                            )
                        # Replace just the stream (works across recent PyMuPDF versions)
                        work.update_stream(xref, new_bytes)
                    except Exception as e:
                        logger.warning(f"Image xref {xref} recompress skipped: {e}")

                    # progress to ~80%
                    jobs[job_id]["progress"] = 20 + int((idx / max(1, total)) * 60)

                # 4) Save intermediate bytes (compact structure)
                jobs[job_id]["status"] = "Saving (PyMuPDF)..."
                jobs[job_id]["progress"] = 82
                interm = io.BytesIO()
                # garbage >= 4 does aggressive xref cleanup; deflate compresses streams; clean removes unused
                work.save(interm, garbage=4, deflate=True, clean=True)
                pymupdf_bytes = interm.getvalue()

        # 5) Ghostscript re-distill (biggest reduction usually happens here)
        jobs[job_id]["status"] = "Applying Ghostscript compression..."
        jobs[job_id]["progress"] = 90
        gs_bytes = ghostscript_compress(pymupdf_bytes, extreme=extreme_compression)

        # 6) PikePDF/QPDF optimize and linearize (fast web view)
        jobs[job_id]["status"] = "Optimizing final PDF structure..."
        jobs[job_id]["progress"] = 95
        final_bytes = pikepdf_optimize(gs_bytes)

        jobs[job_id].update({
            "status": "done",
            "progress": 100,
            "output_buffer": io.BytesIO(final_bytes),
            "error": None
        })
        logger.info(f"Job {job_id} complete. Final size: {len(final_bytes)/1024/1024:.2f} MB")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        jobs[job_id].update({"status": "error", "error": str(e)})

# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/analyze", methods=["POST"])
def analyze():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify(error="Please upload a PDF file"), 400

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as t:
            tmp = t.name
            f.save(t)
        analysis = analyze_pdf_pages(tmp)
        if not analysis:
            return jsonify(error="Failed to analyze PDF"), 500
        return jsonify(analysis)
    finally:
        cleanup_temp_file(tmp)

@app.route("/compress", methods=["POST"])
def compress():
    if "file" not in request.files:
        return jsonify(error="No file part"), 400
    f = request.files["file"]
    if f.filename == "" or not f.filename.lower().endswith(".pdf"):
        return jsonify(error="Please upload a PDF file"), 400

    inp_path = None
    try:
        quality = int(request.form.get("quality", 60))
        max_dimension = int(request.form.get("max_dimension", 1000))
        drawing_mode = request.form.get("drawing_mode", "general")
        extract_pages_str = request.form.get("extract_pages", "").strip()
        extreme_flag = request.form.get("extreme_compression", "false").lower()
        extreme_compression = extreme_flag in ("1", "true", "yes", "on")

        extract_pages = []
        if extract_pages_str:
            extract_pages = [int(p) for p in extract_pages_str.split(",") if p.strip().isdigit()]

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as t:
            inp_path = t.name
            f.save(t)

        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "created_at": time.time(),
            "input_path": inp_path,
            "error": None,
        }

        th = threading.Thread(
            target=process_job,
            args=(job_id, inp_path, quality, max_dimension, drawing_mode, extract_pages, extreme_compression),
            daemon=True,
        )
        th.start()
        return jsonify(job_id=job_id)
    except Exception as e:
        logger.error(f"/compress error: {e}", exc_info=True)
        cleanup_temp_file(inp_path)
        return jsonify(error=str(e)), 500

@app.route("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found", "status": "error"}), 404
    return jsonify({
        "status": job.get("status", "unknown"),
        "progress": int(job.get("progress", 0)),
        "error": job.get("error"),
    })

@app.route("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done" or not job.get("output_buffer"):
        return jsonify(error="File not ready or found"), 404
    buf: io.BytesIO = job["output_buffer"]
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="extracted_drawing.pdf",
        mimetype="application/pdf"
    )

# -----------------------------
# Cleanup worker
# -----------------------------
def cleanup_old_jobs(max_age_sec: int = 3600) -> None:
    now = time.time()
    to_remove = [jid for jid, j in jobs.items() if now - j.get("created_at", now) > max_age_sec]
    for jid in to_remove:
        try:
            job = jobs.pop(jid, None)
            if job and job.get("input_path"):
                cleanup_temp_file(job["input_path"])
        except Exception as e:
            logger.warning(f"Cleanup error for job {jid}: {e}")

def cleanup_worker():
    while True:
        time.sleep(600)
        try:
            cleanup_old_jobs()
        except Exception as e:
            logger.warning(f"Cleanup thread warning: {e}")

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    logger.info("Starting PDF Compressor for Architectural Drawings")
    threading.Thread(target=cleanup_worker, daemon=True).start()
    # For production, set debug=False and run behind a WSGI server (gunicorn/uvicorn+ASGI via asgiref if desired)
    app.run(host="0.0.0.0", port=5001, debug=True)
