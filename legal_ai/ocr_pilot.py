from __future__ import annotations

"""Local OCR execution with auditable cache, timeout, and checkpoint rules.

This module never enables a book, runs canonical processing, or treats OCR as
verified text.  It only writes raw, page-scoped OCR records.
"""

import hashlib
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .domain import InventoryRecord, LegalAIError
from .inventory import file_sha256
from .ocr import load_ocr_pages, ocr_pages_path, write_ocr_pages
from .ocr_exclusions import load_ocr_page_exclusions


PILOT_SCHEMA_VERSION = "ocr-pilot-v1"
DEFAULT_RENDER_DPI = 300
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_CHECKPOINT_EVERY = 10
ENGINE_NAME = "rapidocr"
MODEL_ID = "rapidocr-default-small"


def select_random_pages(*, pdf_page_count: int, count: int, seed: int) -> list[int]:
    """Return a reproducible, uniformly sampled set of one-based PDF pages."""

    if count <= 0:
        raise LegalAIError("Random OCR page count must be positive")
    if count > pdf_page_count:
        raise LegalAIError("Random OCR page count exceeds PDF page count")
    return sorted(random.Random(seed).sample(range(1, pdf_page_count + 1), count))


def _metadata_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError as exc:
        raise LegalAIError(
            "OCR dependencies are missing; run .\\scripts\\bootstrap_ocr.ps1 first"
        ) from exc


def ocr_runtime_info() -> dict[str, str]:
    """Confirm that project-local PDF rendering and CPU OCR are available."""

    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LegalAIError("PyMuPDF is missing; run .\\scripts\\bootstrap_ocr.ps1") from exc
    try:
        import rapidocr  # type: ignore[import-not-found]  # noqa: F401
        import onnxruntime  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:
        raise LegalAIError(
            "RapidOCR/ONNX Runtime is missing; run .\\scripts\\bootstrap_ocr.ps1"
        ) from exc
    return {
        "renderer": "pymupdf",
        "renderer_version": getattr(pymupdf, "__version__", _metadata_version("PyMuPDF")),
        "ocr_engine": ENGINE_NAME,
        "ocr_engine_version": _metadata_version("rapidocr"),
        "ocr_model_id": MODEL_ID,
        "onnxruntime_version": _metadata_version("onnxruntime"),
    }


def render_cache_key(
    *,
    source_pdf_sha256: str,
    pdf_page: int,
    runtime: Mapping[str, str],
    render_dpi: int,
    preprocess_profile: str,
) -> str:
    """Hash every render/OCR input that can affect page text or geometry."""

    payload = {
        "source_pdf_sha256": source_pdf_sha256,
        "pdf_page": pdf_page,
        "renderer": runtime["renderer"],
        "renderer_version": runtime["renderer_version"],
        "ocr_engine": runtime["ocr_engine"],
        "ocr_engine_version": runtime["ocr_engine_version"],
        "ocr_model_id": runtime["ocr_model_id"],
        "render_dpi": render_dpi,
        "preprocess_profile": preprocess_profile,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_path(project_root: Path, book_id: str, pdf_page: int, cache_key: str) -> Path:
    return (
        project_root
        / "data"
        / "raw"
        / "ocr"
        / "renders"
        / book_id
        / f"p{pdf_page:04d}-{cache_key}.png"
    )


def render_page(
    *, pdf_path: Path, output_path: Path, pdf_page: int, dpi: int
) -> tuple[str, bool]:
    """Render one PDF page to a cached PNG and return its hash and reuse flag."""

    if output_path.exists():
        return file_sha256(output_path), True
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - checked by ocr_runtime_info
        raise LegalAIError("PyMuPDF is missing") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.stem + ".tmp.png")
    try:
        with pymupdf.open(pdf_path) as document:
            page = document.load_page(pdf_page - 1)
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            pixmap.save(temporary)
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return file_sha256(output_path), False


def _normalise_bbox(value: Any) -> list[int]:
    """Convert RapidOCR's four-point box into [x1, y1, x2, y2]."""

    try:
        points = list(value)
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (IndexError, TypeError, ValueError) as exc:
        raise LegalAIError("OCR returned an unreadable text-block coordinate") from exc
    if not xs or not ys:
        raise LegalAIError("OCR returned an empty text-block coordinate")
    x1, x2 = int(min(xs)), int(max(xs))
    y1, y2 = int(min(ys)), int(max(ys))
    if x1 >= x2 or y1 >= y2:
        raise LegalAIError("OCR returned an invalid text-block coordinate")
    return [x1, y1, x2, y2]


def order_blocks(raw_blocks: Iterable[Mapping[str, Any]], *, page_id: str) -> list[dict[str, Any]]:
    """Apply the explicit top-to-bottom, then left-to-right reading order rule."""

    cleaned: list[dict[str, Any]] = []
    for raw in raw_blocks:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        bbox = _normalise_bbox(raw.get("bbox"))
        confidence = float(raw.get("confidence", 0.0))
        if not 0.0 < confidence <= 1.0:
            continue
        cleaned.append({"text": text, "bbox": bbox, "confidence": confidence})
    cleaned.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    return [
        {
            "block_id": f"{page_id}:b{position:04d}",
            "text": block["text"],
            "bbox": block["bbox"],
            "confidence": block["confidence"],
            "reading_order": position,
            "role": "body",
        }
        for position, block in enumerate(cleaned, start=1)
    ]


class PersistentOCRWorker:
    """One local OCR engine with per-page deadlines and restart-on-failure."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_request_id = 1

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        self._responses = queue.Queue()
        self._process = subprocess.Popen(
            [sys.executable, "-m", "legal_ai.ocr_worker", "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            env=environment,
        )
        process = self._process
        assert process.stdout is not None

        def read_responses() -> None:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {
                        "ok": False,
                        "error": {
                            "code": "ocr_worker_protocol",
                            "message": "persistent worker returned invalid JSON",
                        },
                    }
                self._responses.put(payload)

        threading.Thread(target=read_responses, daemon=True).start()

    def recognise(self, *, image_path: Path, timeout_seconds: int) -> dict[str, Any]:
        """Return one worker response or terminate the worker at the deadline."""

        try:
            self._start()
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                raise RuntimeError("persistent OCR worker did not start")
            request_id = self._next_request_id
            self._next_request_id += 1
            process.stdin.write(
                json.dumps({"request_id": request_id, "image": str(image_path)}, ensure_ascii=False)
                + "\n"
            )
            process.stdin.flush()
        except (OSError, RuntimeError, ValueError) as exc:
            self.close()
            return {
                "ok": False,
                "error": {
                    "code": "ocr_worker_start_error",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                },
            }

        try:
            response = self._responses.get(timeout=timeout_seconds)
        except queue.Empty:
            self.close()
            return {
                "ok": False,
                "error": {
                    "code": "ocr_timeout",
                    "message": f"OCR exceeded {timeout_seconds} seconds; worker restarted",
                },
            }
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            self.close()
            return {
                "ok": False,
                "error": {
                    "code": "ocr_worker_protocol",
                    "message": "persistent worker returned an unmatched response",
                },
            }
        response.pop("request_id", None)
        if not isinstance(response.get("ok"), bool):
            self.close()
            return {
                "ok": False,
                "error": {
                    "code": "ocr_worker_protocol",
                    "message": "persistent worker returned an invalid response",
                },
            }
        return response

    def close(self) -> None:
        """Stop the worker; safe after either a timeout or a successful run."""

        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _failure_row(
    *,
    record: InventoryRecord,
    pdf_page: int,
    runtime: Mapping[str, str],
    dpi: int,
    profile: str,
    attempts: int,
    error: Mapping[str, Any],
    cache_key: str | None = None,
    image_sha256: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": "ocr-page-v1",
        "page_id": f"{record.book.book_id}:p{pdf_page:04d}",
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "failed",
        "ocr_engine": runtime["ocr_engine"],
        "ocr_engine_version": runtime["ocr_engine_version"],
        "ocr_model_id": runtime["ocr_model_id"],
        "render_dpi": dpi,
        "preprocess_profile": profile,
        "ocr_text": None,
        "ocr_text_sha256": None,
        "ocr_avg_confidence": None,
        "blocks": [],
        "error": {
            "code": str(error.get("code", "ocr_error")),
            "message": str(error.get("message", "OCR failed"))[:500],
        },
        "skip_reason": None,
        "ocr_attempts": attempts,
    }
    if cache_key is not None:
        row["render_cache_key"] = cache_key
    if image_sha256 is not None:
        row["render_image_sha256"] = image_sha256
    return row


def _skipped_row(
    *, record: InventoryRecord, pdf_page: int, dpi: int, profile: str, reason: str
) -> dict[str, Any]:
    return {
        "schema_version": "ocr-page-v1",
        "page_id": f"{record.book.book_id}:p{pdf_page:04d}",
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "skipped",
        "ocr_engine": None,
        "ocr_engine_version": None,
        "ocr_model_id": None,
        "render_dpi": dpi,
        "preprocess_profile": profile,
        "ocr_text": None,
        "ocr_text_sha256": None,
        "ocr_avg_confidence": None,
        "blocks": [],
        "error": None,
        "skip_reason": reason,
    }


def _generated_row(
    *,
    record: InventoryRecord,
    pdf_page: int,
    runtime: Mapping[str, str],
    dpi: int,
    profile: str,
    attempts: int,
    cache_key: str,
    image_sha256: str,
    raw_blocks: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    page_id = f"{record.book.book_id}:p{pdf_page:04d}"
    blocks = order_blocks(raw_blocks, page_id=page_id)
    if not blocks:
        return _failure_row(
            record=record,
            pdf_page=pdf_page,
            runtime=runtime,
            dpi=dpi,
            profile=profile,
            attempts=attempts,
            cache_key=cache_key,
            image_sha256=image_sha256,
            error={"code": "ocr_empty_result", "message": "OCR returned no usable text blocks"},
        )
    text = "\n".join(str(block["text"]) for block in blocks)
    if "\ufffd" in text:
        return _failure_row(
            record=record,
            pdf_page=pdf_page,
            runtime=runtime,
            dpi=dpi,
            profile=profile,
            attempts=attempts,
            cache_key=cache_key,
            image_sha256=image_sha256,
            error={
                "code": "ocr_text_encoding_error",
                "message": "OCR worker returned a Unicode replacement character",
            },
        )
    return {
        "schema_version": "ocr-page-v1",
        "page_id": page_id,
        "book_id": record.book.book_id,
        "pdf_page": pdf_page,
        "source_pdf_sha256": record.sha256,
        "ocr_status": "generated",
        "ocr_engine": runtime["ocr_engine"],
        "ocr_engine_version": runtime["ocr_engine_version"],
        "ocr_model_id": runtime["ocr_model_id"],
        "render_dpi": dpi,
        "preprocess_profile": profile,
        "ocr_text": text,
        "ocr_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "ocr_avg_confidence": sum(float(block["confidence"]) for block in blocks) / len(blocks),
        "blocks": blocks,
        "error": None,
        "skip_reason": None,
        "ocr_attempts": attempts,
        "render_cache_key": cache_key,
        "render_image_sha256": image_sha256,
    }


def run_ocr_pilot(
    *,
    record: InventoryRecord,
    project_root: Path,
    pages: Iterable[int],
    dpi: int = DEFAULT_RENDER_DPI,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    force: bool = False,
    scope: str = "bounded_ocr_pilot",
    registered_skips: Mapping[int, str] | None = None,
    progress: Callable[[str], None] | None = None,
    progress_state_path: Path | None = None,
) -> dict[str, Any]:
    """OCR explicit pages, atomically checkpointing every bounded batch."""

    if record.book.ocr_policy == "off" or not record.book.ocr_profile:
        raise LegalAIError(f"{record.book.book_id} has no executable OCR policy")
    if not 72 <= dpi <= 1200:
        raise LegalAIError("OCR render DPI must be between 72 and 1200")
    if timeout_seconds <= 0 or max_attempts <= 0 or checkpoint_every <= 0:
        raise LegalAIError("OCR timeout, attempts, and checkpoint size must be positive")

    chosen_pages = sorted(set(pages))
    if not chosen_pages:
        raise LegalAIError("At least one OCR page must be selected")
    if chosen_pages[0] < 1 or chosen_pages[-1] > record.actual_pdf_pages:
        raise LegalAIError("OCR page is outside the source PDF")
    skip_reasons = dict(registered_skips or {})
    if set(chosen_pages) & set(skip_reasons):
        raise LegalAIError("An OCR page cannot be both selected and registered as skipped")
    if any(not isinstance(reason, str) or not reason.strip() for reason in skip_reasons.values()):
        raise LegalAIError("Registered OCR skips require non-empty reasons")

    runtime = ocr_runtime_info()
    profile = record.book.ocr_profile
    output_path = ocr_pages_path(project_root, record.book.book_id)
    existing = load_ocr_pages(
        output_path,
        book_id=record.book.book_id,
        source_pdf_sha256=record.sha256,
        pdf_page_count=record.actual_pdf_pages,
    )
    started = time.monotonic()
    run_started_at = datetime.now(timezone.utc).isoformat()
    page_reports: list[dict[str, Any]] = []
    registered_exclusions: list[dict[str, Any]] = []
    updates: dict[int, dict[str, Any]] = {}

    def checkpoint(*, latest_page: int | None) -> None:
        merged = {**existing, **updates}
        write_ocr_pages(
            output_path,
            merged.values(),
            book_id=record.book.book_id,
            source_pdf_sha256=record.sha256,
            pdf_page_count=record.actual_pdf_pages,
        )
        if progress_state_path is not None:
            write_ocr_pilot_report(
                progress_state_path,
                {
                    "schema_version": "ocr-batch-progress-v1",
                    "run_started_at": run_started_at,
                    "scope": scope,
                    "book_id": record.book.book_id,
                    "source_pdf_sha256": record.sha256,
                    "selected_page_count": len(chosen_pages),
                    "registered_skip_count": len(skip_reasons),
                    "persisted_row_count": len(merged),
                    "updated_row_count": len(updates),
                    "latest_pdf_page": latest_page,
                    "raw_ocr_jsonl": str(output_path.relative_to(project_root)),
                },
            )

    for pdf_page, reason in sorted(skip_reasons.items()):
        if not 1 <= pdf_page <= record.actual_pdf_pages:
            raise LegalAIError("Registered OCR skip is outside the source PDF")
        prior = existing.get(pdf_page)
        if prior is None or prior.get("ocr_status") == "failed":
            updates[pdf_page] = _skipped_row(
                record=record, pdf_page=pdf_page, dpi=dpi, profile=profile, reason=reason
            )
            outcome = "registered_exclusion"
            status = "skipped"
        else:
            outcome = "retained_audit_record"
            status = str(prior["ocr_status"])
        registered_exclusions.append(
            {"pdf_page": pdf_page, "ocr_status": status, "reason": reason, "outcome": outcome}
        )

    worker = PersistentOCRWorker()
    latest_page: int | None = None
    try:
        for position, pdf_page in enumerate(chosen_pages, start=1):
            latest_page = pdf_page
            cache_key = render_cache_key(
                source_pdf_sha256=record.sha256,
                pdf_page=pdf_page,
                runtime=runtime,
                render_dpi=dpi,
                preprocess_profile=profile,
            )
            image_path = _render_path(project_root, record.book.book_id, pdf_page, cache_key)
            prior = existing.get(pdf_page)
            if (
                not force
                and prior is not None
                and prior.get("ocr_status") == "generated"
                and prior.get("render_cache_key") == cache_key
                and image_path.exists()
                and prior.get("render_image_sha256") == file_sha256(image_path)
            ):
                result = {
                    "pdf_page": pdf_page,
                    "ocr_status": "generated",
                    "outcome": "reused_existing_ocr",
                    "block_count": len(prior["blocks"]),
                    "ocr_avg_confidence": prior["ocr_avg_confidence"],
                    "ocr_text_sha256": prior["ocr_text_sha256"],
                    "render_cache_key": cache_key,
                }
                page_reports.append(result)
                if progress is not None:
                    progress(f"OCR {record.book.book_id}: {position}/{len(chosen_pages)} page {pdf_page} reused")
                continue

            image_sha256: str | None = None
            try:
                image_sha256, render_reused = render_page(
                    pdf_path=record.absolute_path,
                    output_path=image_path,
                    pdf_page=pdf_page,
                    dpi=dpi,
                )
            except Exception as exc:
                row = _failure_row(
                    record=record,
                    pdf_page=pdf_page,
                    runtime=runtime,
                    dpi=dpi,
                    profile=profile,
                    attempts=1,
                    cache_key=cache_key,
                    error={"code": "render_error", "message": f"{type(exc).__name__}: {exc}"},
                )
                updates[pdf_page] = row
                result = {
                    "pdf_page": pdf_page,
                    "ocr_status": "failed",
                    "outcome": "render_error",
                    "error_code": row["error"]["code"],
                    "render_cache_key": cache_key,
                }
                page_reports.append(result)
                if progress is not None:
                    progress(f"OCR {record.book.book_id}: {position}/{len(chosen_pages)} page {pdf_page} render failed")
                if len(updates) % checkpoint_every == 0:
                    checkpoint(latest_page=latest_page)
                continue

            response: dict[str, Any] = {}
            attempts = 0
            for attempts in range(1, max_attempts + 1):
                response = worker.recognise(image_path=image_path, timeout_seconds=timeout_seconds)
                if response.get("ok"):
                    break
            if response.get("ok") and not isinstance(response.get("blocks"), list):
                response = {
                    "ok": False,
                    "error": {"code": "ocr_worker_protocol", "message": "worker did not return a blocks array"},
                }
            if response.get("ok"):
                row = _generated_row(
                    record=record,
                    pdf_page=pdf_page,
                    runtime=runtime,
                    dpi=dpi,
                    profile=profile,
                    attempts=attempts,
                    cache_key=cache_key,
                    image_sha256=image_sha256,
                    raw_blocks=response["blocks"],
                )
            else:
                row = _failure_row(
                    record=record,
                    pdf_page=pdf_page,
                    runtime=runtime,
                    dpi=dpi,
                    profile=profile,
                    attempts=attempts,
                    cache_key=cache_key,
                    image_sha256=image_sha256,
                    error=response.get("error", {"code": "ocr_error", "message": "OCR failed"}),
                )
            updates[pdf_page] = row
            result = {
                "pdf_page": pdf_page,
                "ocr_status": row["ocr_status"],
                "outcome": "render_reused" if render_reused else "rendered",
                "attempts": row["ocr_attempts"],
                "block_count": len(row["blocks"]),
                "ocr_avg_confidence": row["ocr_avg_confidence"],
                "ocr_text_sha256": row["ocr_text_sha256"],
                "render_cache_key": cache_key,
                "error_code": None if row["error"] is None else row["error"]["code"],
            }
            page_reports.append(result)
            if progress is not None:
                progress(
                    f"OCR {record.book.book_id}: {position}/{len(chosen_pages)} page {pdf_page} {row['ocr_status']}"
                )
            if len(updates) % checkpoint_every == 0:
                checkpoint(latest_page=latest_page)
    finally:
        checkpoint(latest_page=latest_page)
        worker.close()

    source_pdf_sha256_after = file_sha256(record.absolute_path)
    generated = sum(item["ocr_status"] == "generated" for item in page_reports)
    failed = sum(item["ocr_status"] == "failed" for item in page_reports)
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "run_started_at": run_started_at,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": scope,
        "book": {
            "book_id": record.book.book_id,
            "pdf_page_count": record.actual_pdf_pages,
            "source_pdf_sha256": record.sha256,
            "source_pdf_unchanged": source_pdf_sha256_after == record.sha256,
        },
        "runtime": runtime,
        "parameters": {
            "render_dpi": dpi,
            "preprocess_profile": profile,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "checkpoint_every": checkpoint_every,
        },
        "raw_ocr_jsonl": str(output_path.relative_to(project_root)),
        "summary": {
            "requested_pages": len(chosen_pages),
            "generated_pages": generated,
            "failed_pages": failed,
            "reused_pages": sum(item["outcome"] == "reused_existing_ocr" for item in page_reports),
            "registered_skipped_pages": sum(
                item["outcome"] == "registered_exclusion" for item in registered_exclusions
            ),
            "all_generated_text_is_ocr_unverified": True,
        },
        "registered_exclusions": registered_exclusions,
        "pages": page_reports,
    }


def write_ocr_pilot_report(path: Path, report: Mapping[str, Any]) -> None:
    """Write an audit report without placing OCR text in reports/."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
