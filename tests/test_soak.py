"""Spec section 12, test 6: soak test.

At least 15 minutes of continuous audio. Assert no memory growth, no stuck
states, no crashes, and report the latency distribution across the whole run.

Runs in real time by design -- pacing at 1x is the only way the latency
figures and the "no stuck states" claim mean anything.

    .venv/bin/python -m pytest tests/test_soak.py -s -m slow
"""
from __future__ import annotations

import asyncio
import gc
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="numpy not installed")
pytest.importorskip("mlx.core", reason="mlx not installed")
pytest.importorskip("parakeet_mlx", reason="parakeet-mlx not installed")

from tests.harness import (AUDIO_DIR, OfflineRun, load_wav, percentile, rss_kb,
                           silence)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPORT = Path(__file__).resolve().parent.parent / "docs" / "test-results.json"
SOAK_MINUTES = float(__import__("os").environ.get("SOAK_MINUTES", "15"))


def _write_report(key, payload):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if REPORT.exists():
        try:
            data = json.loads(REPORT.read_text())
        except json.JSONDecodeError:
            data = {}
    data[key] = payload
    REPORT.write_text(json.dumps(data, indent=2, ensure_ascii=False))


@pytest.mark.asyncio
async def test_continuous_audio_soak(cfg):
    speech = [load_wav(AUDIO_DIR / n) for n in ("en_technical.wav", "en_casual.wav")]
    gap = silence(0.6)

    target_s = SOAK_MINUTES * 60.0
    started = time.perf_counter()
    samples: list[tuple[float, int]] = []      # (elapsed_s, rss_kb)

    async with OfflineRun(cfg) as run:
        gc.collect()
        baseline_rss = rss_kb()
        await run.feed(silence(0.4))

        i = 0
        while time.perf_counter() - started < target_s:
            await run.feed(speech[i % len(speech)])
            await run.feed(gap)
            i += 1
            samples.append((round(time.perf_counter() - started, 1), rss_kb()))

        await run.settle(timeout=90)
        gc.collect()
        await asyncio.sleep(1.0)
        final_rss = rss_kb()

        # Liveness must be checked here, inside the context: leaving it stops
        # the pipeline and clears the worker.
        worker_alive = (
            run.pipeline._worker is not None and not run.pipeline._worker.done()
        )
        queue_drained = run.pipeline._queue.empty()
        asr_alive = run.asr.running

    elapsed = time.perf_counter() - started
    first = run.pipeline.first_char_latencies
    total = run.pipeline.total_latencies
    lines = run.server.final_lines

    # Memory: compare the second half's mean against the first half's. A leak
    # over 15 minutes of continuous decoding shows up plainly here; transient
    # allocator noise does not.
    # Latency across the first vs second half of the run. A system that is
    # slowly wedging shows up here even when absolute numbers look fine.
    mid = max(1, len(first) // 2)
    first_half_lat = percentile(first[:mid], 50) if first else float("nan")
    second_half_lat = percentile(first[mid:], 50) if first else float("nan")

    rss_values = [r for _, r in samples]
    half = max(1, len(rss_values) // 2)
    first_half = sum(rss_values[:half]) / half
    second_half = sum(rss_values[half:]) / max(1, len(rss_values) - half)
    growth_pct = (second_half - first_half) / first_half * 100.0

    stats = {
        "minutes": round(elapsed / 60.0, 2),
        "passes": i,
        "chunks_translated": len(total),
        "lines_displayed": len(lines),
        "rss_kb": {
            "baseline": baseline_rss,
            "first_half_mean": round(first_half),
            "second_half_mean": round(second_half),
            "final": final_rss,
            "peak": max(rss_values) if rss_values else 0,
            "growth_pct_half_over_half": round(growth_pct, 2),
        },
        "first_char_ms": {
            "median": round(percentile(first, 50), 1) if first else None,
            "p90": round(percentile(first, 90), 1) if first else None,
            "p99": round(percentile(first, 99), 1) if first else None,
            "min": round(min(first), 1) if first else None,
            "max": round(max(first), 1) if first else None,
            "n": len(first),
        },
        "full_line_ms": {
            "median": round(percentile(total, 50), 1) if total else None,
            "p90": round(percentile(total, 90), 1) if total else None,
            "max": round(max(total), 1) if total else None,
        },
        "latency_drift": {
            "first_half_median_ms": round(first_half_lat, 1),
            "second_half_median_ms": round(second_half_lat, 1),
            "ratio": round(second_half_lat / first_half_lat, 3),
        },
        "gate_stats": dict(run.pipeline.gate.stats),
    }
    _write_report("test6_soak", stats)
    print("\n--- test 6: soak ---")
    print(json.dumps(stats, indent=2, ensure_ascii=False))

    assert elapsed >= target_s, f"soak only ran {elapsed/60:.1f} min"
    # No crash, no stuck state.
    assert worker_alive, "translation worker died during the soak"
    assert asr_alive, "ASR worker died during the soak"
    assert queue_drained, "queue did not drain: stuck state"
    assert len(total) > 0, "no translations completed during the soak"
    assert lines, "nothing was displayed during the soak"
    # No unbounded memory growth.
    assert growth_pct < 12.0, f"RSS grew {growth_pct:.1f}% across the run"
    # The real degradation test: latency must not creep upward across the run.
    # This catches a slowly wedging pipeline that an absolute bound would miss.
    assert second_half_lat < first_half_lat * 1.6, (
        f"latency drifted upward: {first_half_lat:.0f}ms -> {second_half_lat:.0f}ms"
    )
    # Absolute sanity bound. The soak feeds speech back to back with only 0.6s
    # gaps, which is harsher than a person talking, so this sits above the
    # ~2.7s p90 measured under that load rather than at it.
    assert percentile(first, 90) < 4000.0, \
        f"p90 first-character latency degraded to {percentile(first,90):.0f}ms"
