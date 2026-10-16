from __future__ import annotations

import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from PIL import Image


@dataclass
class PageContent:
    page_number: int
    text: str
    source: str
    ocr_confidence: float | None = None
    image_jpeg: bytes | None = None


@dataclass
class PDFDocument:
    path: Path
    pages: list[PageContent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(
            f"--- PAGE {p.page_number} ({p.source}) ---\n{p.text}"
            for p in self.pages
        )

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def mean_ocr_confidence(self) -> float | None:
        values = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        return sum(values) / len(values) if values else None

    @staticmethod
    def table_score(text: str) -> int:
        """Count numeric-dense rows; OCR confidence alone misses column mixups."""
        lines = [re.sub(r"\s+", " ", row).strip() for row in text.splitlines() if row.strip()]
        numeric_rows = 0
        for line in lines:
            numbers = re.findall(r"(?<!\w)[(+-]?\d[\d.,'’ ]*(?:\d)?%?\)?(?!\w)", line)
            if len(numbers) >= 3:
                numeric_rows += 1
        return numeric_rows

    @staticmethod
    def _looks_like_table(text: str) -> bool:
        """Return true when repeated numeric-dense rows suggest an item table."""
        return PDFDocument.table_score(text) >= 3

    @property
    def needs_vision(self) -> bool:
        """Whether visual layout is likely to add value, not just whether OCR is short."""
        if not self.pages or len(self.text.strip()) < 350:
            return True
        for page in self.pages:
            if page.source == "image_only" or "ocr_skipped" in page.source:
                return True
            if page.source.startswith("tesseract_ocr") and (
                page.ocr_confidence is None or page.ocr_confidence < 0.82
            ):
                return True
            if self._looks_like_table(page.text):
                return True
        return False


def _render_page(page: fitz.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    image.thumbnail((1800, 2400))
    return image


def _image_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue()


_TESSERACT_CONFIGURED = False


def _ensure_tesseract() -> None:
    """Point pytesseract at a Tesseract binary when it is not on PATH.

    Honours TESSERACT_CMD, then common install locations (Windows/macOS/Linux).
    Silent on failure: the caller reports OCR as unavailable and the pipeline
    degrades to the vision route instead of pretending OCR text exists.
    """
    global _TESSERACT_CONFIGURED
    if _TESSERACT_CONFIGURED:
        return
    try:
        import pytesseract
        import shutil

        explicit = os.getenv("TESSERACT_CMD", "").strip()
        if explicit and os.path.isfile(explicit):
            pytesseract.pytesseract.tesseract_cmd = explicit
        elif not shutil.which("tesseract"):
            candidates = (
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
                "/opt/homebrew/bin/tesseract",
                "/usr/local/bin/tesseract",
                "/usr/bin/tesseract",
            )
            for candidate in candidates:
                if os.path.isfile(candidate):
                    pytesseract.pytesseract.tesseract_cmd = candidate
                    break
    except Exception:
        pass
    finally:
        _TESSERACT_CONFIGURED = True


def _ocr_image(image: Image.Image) -> tuple[str, float | None, str | None]:
    try:
        _ensure_tesseract()
        import pytesseract
        from pytesseract import Output

        timeout_seconds = max(5, min(30, int(os.getenv("OCR_PAGE_TIMEOUT_SECONDS", "15"))))
        data = pytesseract.image_to_data(
            image, output_type=Output.DICT, config="--psm 6", timeout=timeout_seconds
        )
        confidences: list[float] = []
        lines: dict[tuple[int, int, int], list[str]] = {}
        count = len(data.get("text", []))
        for i in range(count):
            try:
                confidence = float(data.get("conf", ["-1"] * count)[i])
                if confidence >= 0:
                    confidences.append(confidence)
            except (TypeError, ValueError):
                pass
            word = str(data.get("text", [""] * count)[i]).strip()
            if not word:
                continue
            key = (
                int(data.get("block_num", [0] * count)[i]),
                int(data.get("par_num", [0] * count)[i]),
                int(data.get("line_num", [0] * count)[i]),
            )
            lines.setdefault(key, []).append(word)
        text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
        score = sum(confidences) / len(confidences) / 100 if confidences else None
        return text.strip(), score, None
    except Exception as exc:
        return "", None, f"Local OCR unavailable: {type(exc).__name__}: {exc}"


def read_pdf(path: Path, dpi: int = 160, max_vision_pages: int = 20, ocr_mode: str = "auto") -> PDFDocument:
    """Extract searchable text and render every page for optional visual inspection.

    Native extraction and rendering are sequential because PyMuPDF page objects are not
    shared across threads. OCR then runs in a bounded thread pool over detached PIL images,
    avoiding serial multi-minute waits on long scanned PDFs.
    """
    result = PDFDocument(path=path)
    mode = (ocr_mode or "auto").strip().lower()
    if mode not in {"auto", "all", "sample", "off"}:
        raise ValueError("ocr_mode must be auto, all, sample, or off")
    if mode == "auto":
        # When Gemini is configured, it can inspect every rendered page directly; sample
        # OCR is used for routing/evidence and full OCR is deferred until a text-only fallback.
        mode = "off" if os.getenv("GEMINI_API_KEY", "").strip() else "all"
    try:
        pdf = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"Cannot open PDF {path.name}: {exc}") from exc
    if pdf.page_count == 0:
        pdf.close()
        raise RuntimeError(f"PDF {path.name} has no pages")

    pending_ocr: dict[int, Image.Image] = {}
    page_build: list[dict[str, object]] = []
    try:
        for idx, page in enumerate(pdf):
            native = (page.get_text("text") or "").strip()
            compact_native = re.sub(r"\s+", "", native)
            image = _render_page(page, dpi)
            render_needed = len(compact_native) < 100
            page_build.append({
                "page_number": idx + 1,
                "native": native,
                "image": image,
                "render_needed": render_needed,
            })
            if render_needed:
                page_build[-1]["needs_ocr"] = True
                page_build[-1]["image_jpeg"] = _image_bytes(image)
            else:
                page_build[-1]["needs_ocr"] = False
                page_build[-1]["image_jpeg"] = _image_bytes(image)
    finally:
        pdf.close()

    sparse_pages = [int(row["page_number"]) for row in page_build if bool(row["needs_ocr"])]
    if mode == "all":
        selected_ocr_pages = set(sparse_pages)
    elif mode == "off":
        selected_ocr_pages = set()
    else:  # sample mode: first, last and evenly spaced pages
        count = max(2, min(6, int(os.getenv("OCR_SAMPLE_PAGES", "2"))))
        if len(sparse_pages) <= count:
            selected_ocr_pages = set(sparse_pages)
        else:
            indices = {0, len(sparse_pages) - 1}
            for slot in range(max(0, count - 2)):
                indices.add(round((slot + 1) * (len(sparse_pages) - 1) / (count - 1)))
            selected_ocr_pages = {sparse_pages[i] for i in sorted(indices)}
    for row in page_build:
        page_no = int(row["page_number"])
        if page_no in selected_ocr_pages:
            pending_ocr[page_no] = row["image"]  # type: ignore[assignment]

    ocr_results: dict[int, tuple[str, float | None, str | None]] = {}
    if pending_ocr:
        worker_count = max(1, min(6, int(os.getenv("OCR_WORKERS", "4"))))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="invoice-ocr") as pool:
            future_to_page = {pool.submit(_ocr_image, image): page_no for page_no, image in pending_ocr.items()}
            for future in as_completed(future_to_page):
                page_no = future_to_page[future]
                try:
                    ocr_results[page_no] = future.result()
                except Exception as exc:
                    ocr_results[page_no] = ("", None, f"OCR worker failed: {type(exc).__name__}: {exc}")

    for row in page_build:
        page_no = int(row["page_number"])
        native = str(row["native"] or "")
        image = row["image"]
        render_needed = bool(row["render_needed"])
        text = native
        source = "native_text"
        confidence = None
        if render_needed:
            if page_no not in selected_ocr_pages:
                text = native
                source = "image_only" if not native else "native_text_ocr_skipped"
                if mode != "off":
                    result.warnings.append(f"page {page_no}: OCR skipped by {mode} mode; rendered image retained for visual extraction")
            else:
                ocr_text, confidence, warning = ocr_results.get(page_no, ("", None, "OCR result missing"))
                if warning:
                    result.warnings.append(f"page {page_no}: {warning}")
                if native and ocr_text:
                    text = f"[NATIVE PDF TEXT]\n{native}\n[OCR TEXT]\n{ocr_text}"
                    source = "native_text_plus_ocr"
                elif ocr_text:
                    text = ocr_text
                    source = "tesseract_ocr"
                elif native:
                    text = native
                    source = "native_text_ocr_unavailable"
                else:
                    text = ""
                    source = "image_only"
        result.pages.append(
            PageContent(
                page_number=page_no,
                text=text.strip(),
                source=source,
                ocr_confidence=confidence,
                image_jpeg=bytes(row["image_jpeg"]),
            )
        )

    if mode == "off" and any(page.source == "image_only" for page in result.pages):
        result.warnings.append("Local OCR was skipped for this pass; use Gemini vision. Full OCR is performed on demand if a text-only Groq fallback or evidence-grounded repair is needed.")
    if not any(page.text.strip() for page in result.pages):
        result.warnings.append("No searchable/OCR text found; multimodal extraction is required.")
    if result.page_count > max_vision_pages:
        result.warnings.append(
            f"Document has {result.page_count} pages; visual API calls are capped by MAX_VISION_PAGES={max_vision_pages}. "
            "All pages remain represented in page-labelled text, but omitted page images may reduce table accuracy."
        )
    return result

def ensure_full_ocr(doc: PDFDocument) -> None:
    """OCR image-only pages on demand before using a text-only provider fallback."""
    missing = [p for p in doc.pages if (not p.text.strip() or p.source == "image_only" or "ocr_skipped" in p.source) and p.image_jpeg]
    if not missing:
        return
    worker_count = max(1, min(6, int(os.getenv("OCR_WORKERS", "4"))))
    jobs = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="invoice-ocr-fallback") as pool:
        for page in missing:
            try:
                image = Image.open(io.BytesIO(page.image_jpeg)).convert("RGB")
                jobs[pool.submit(_ocr_image, image)] = page
            except Exception as exc:
                doc.warnings.append(f"page {page.page_number}: could not prepare OCR fallback: {type(exc).__name__}: {exc}")
        for future in as_completed(jobs):
            page = jobs[future]
            try:
                text, confidence, warning = future.result()
            except Exception as exc:
                text, confidence, warning = "", None, f"OCR fallback failed: {type(exc).__name__}: {exc}"
            if text:
                page.text = text
                page.source = "tesseract_ocr_fallback"
                page.ocr_confidence = confidence
            if warning:
                doc.warnings.append(f"page {page.page_number}: {warning}")


def split_document_text(text: str, max_chars: int) -> list[str]:
    """Split page-labelled text into consecutive parts, each under the budget.

    Used by text-only routes: a provider with a small per-request token limit
    (free-tier Groq) cannot read a long scanned dossier in one call, so the
    document is processed part-by-part and the payables are merged.
    """
    if len(text) <= max_chars:
        return [text]
    blocks = [b for b in re.split(r"(?=--- PAGE \d+ \([^\n]+\) ---\n)", text) if b.strip()]
    if len(blocks) <= 1:
        head = int(max_chars * 0.9)
        parts = []
        cursor = 0
        while cursor < len(text):
            parts.append(text[cursor:cursor + head])
            cursor += head
        return parts
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        if current and current_len + len(block) > max_chars:
            parts.append("\n\n".join(current))
            current, current_len = [], 0
        if len(block) > max_chars:
            # A single oversized page: hard-split it with explicit markers.
            if current:
                parts.append("\n\n".join(current))
                current, current_len = [], 0
            head = int(max_chars * 0.6)
            tail = max_chars - head - 60
            parts.append(block[:head] + "\n[... middle of this page omitted (part boundary) ...]\n" + block[-tail:])
            continue
        current.append(block)
        current_len += len(block) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts


def trim_document_text(text: str, max_chars: int) -> str:
    """Trim while preserving every page label and a sample from every page.

    Naive head/tail truncation can completely drop middle item-table pages. This function
    allocates a shared budget per page and marks any local truncation explicitly.
    """
    if len(text) <= max_chars:
        return text
    blocks = re.split(r"(?=--- PAGE \d+ \([^\n]+\) ---\n)", text)
    blocks = [b for b in blocks if b.strip()]
    if len(blocks) <= 1:
        head = int(max_chars * 0.6)
        tail = max_chars - head
        return text[:head] + "\n[... OMITTED ...]\n" + text[-tail:]

    header_cost = sum(len(re.match(r"--- PAGE .*? ---\n", b).group(0)) if re.match(r"--- PAGE .*? ---\n", b) else 40 for b in blocks)
    body_budget = max(400, max_chars - header_cost - 100 * len(blocks))
    per_page_budget = max(140, body_budget // len(blocks))
    shortened: list[str] = []
    for block in blocks:
        if len(block) <= per_page_budget:
            shortened.append(block)
            continue
        match = re.match(r"--- PAGE .*? ---\n", block)
        header = match.group(0) if match else ""
        body = block[len(header):]
        content_budget = max(60, per_page_budget - len(header) - 38)
        left = max(25, int(content_budget * 0.58))
        right = max(25, content_budget - left)
        shortened.append(header + body[:left] + "\n[... middle of this page omitted ...]\n" + body[-right:])
    result = "\n\n".join(shortened)
    if len(result) > max_chars:
        # Safety cap while keeping the page labels distributed in the result.
        return result[:max_chars]
    return result
