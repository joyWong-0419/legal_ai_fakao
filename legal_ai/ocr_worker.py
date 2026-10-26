from __future__ import annotations

"""RapidOCR worker supporting one-shot and persistent JSONL stdin modes."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


def _result_parts(result: Any) -> tuple[list[Any], list[Any], list[Any]]:
    """Support RapidOCR's current object API and the older tuple/list API."""

    if (
        hasattr(result, "boxes")
        and hasattr(result, "txts")
        and hasattr(result, "scores")
    ):
        return _to_list(result.boxes), _to_list(result.txts), _to_list(result.scores)
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, list):
        boxes: list[Any] = []
        texts: list[Any] = []
        scores: list[Any] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            boxes.append(item[0])
            texts.append(item[1][0])
            scores.append(item[1][1])
        return boxes, texts, scores
    raise RuntimeError("RapidOCR returned an unsupported result format")


def _build_engine() -> Any:
    from rapidocr import RapidOCR  # type: ignore[import-not-found]

    return RapidOCR()


def recognise(engine: Any, image_path: Path) -> list[dict[str, Any]]:
    result = engine(str(image_path))
    boxes, texts, scores = _result_parts(result)
    if not (len(boxes) == len(texts) == len(scores)):
        raise RuntimeError("RapidOCR returned mismatched boxes, texts, and scores")
    return [
        {"bbox": box, "text": text, "confidence": score}
        for box, text, score in zip(boxes, texts, scores)
    ]


def _response_for(engine: Any, image_path: Path) -> dict[str, Any]:
    try:
        return {"ok": True, "blocks": recognise(engine, image_path)}
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "ocr_engine_error",
                "message": f"{type(exc).__name__}: {exc}"[:500],
            },
        }


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> int:
    """Keep one model resident and return one JSON object per JSONL request."""

    try:
        engine = _build_engine()
    except Exception as exc:
        _write(
            {
                "ok": False,
                "error": {
                    "code": "ocr_engine_init_error",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                },
            }
        )
        return 1
    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = request["request_id"]
            image_path = Path(request["image"])
            if not isinstance(request_id, int):
                raise ValueError("request_id must be an integer")
            payload = _response_for(engine, image_path)
            payload["request_id"] = request_id
        except Exception as exc:
            payload = {
                "ok": False,
                "error": {
                    "code": "ocr_worker_request_error",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                },
            }
        _write(payload)
    return 0


def main() -> int:
    # The parent always consumes UTF-8, independent of the Windows console code page.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", type=Path)
    mode.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.serve:
        return serve()
    try:
        payload = _response_for(_build_engine(), args.image)
    except Exception as exc:  # pragma: no cover - defensive engine-init fallback
        payload = {
            "ok": False,
            "error": {
                "code": "ocr_engine_init_error",
                "message": f"{type(exc).__name__}: {exc}"[:500],
            },
        }
    _write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
