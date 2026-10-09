"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped JSONL event and mirror it to stdout."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def state_of(region: str) -> dict:
    """Read a region's serving state without treating readiness as liveness."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the target before performing the DNS/LB cutover."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unknown snapshot backend: {backend}")
    if wait < 0:
        raise ValueError("wait cannot be negative")

    try:
        before = state_of(target)
        emit(step="1_verify_target", ok=True, target=target, state=before)
    except httpx.HTTPError as exc:
        before = {"region": target, "error": type(exc).__name__}
        emit(step="1_verify_target", ok=False, target=target, state=before,
             error=str(exc))

    try:
        restored = snapshot.get(target, backend)
    except (Exception, SystemExit) as exc:
        error = str(exc)
        emit(step="2_restore_snapshot", ok=False, target=target, backend=backend,
             error=error, rpo_seconds=None, docs_lost=None,
             embed_model_version=None)
        return {"ok": False, "target": target, "failed_step": "2_restore_snapshot",
                "error": error, "state_before": before}

    source = restored.get("source_region") or ("b" if target == "a" else "a")
    rpo = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True, target=target, source_region=source,
         backend=backend, snapshot_at=restored.get("snapshot_at"),
         restored_at=restored.get("restored_at"),
         rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
         embed_model_version=restored.get("embed_model_version"))

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    previous_pool_state = pool_file.read_text().strip() if pool_file.exists() else "cold"
    pool_file.write_text("full", encoding="utf-8")
    emit(step="3_scale_pool", ok=True, target=target,
         from_state=previous_pool_state, to_state="full")

    started = time.monotonic()
    deadline = started + wait
    attempts = 0
    last_reason = "not_probed"
    ready_body = None
    while True:
        attempts += 1
        remaining = deadline - time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/readyz",
                                 timeout=max(0.05, min(2.0, remaining if remaining > 0 else 0.05)))
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 200:
                ready_body = body
                break
            reasons = body.get("reasons")
            last_reason = ",".join(str(reason) for reason in reasons) if reasons else f"http_{response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}:{exc}"

        if time.monotonic() >= deadline:
            waited = round(time.monotonic() - started, 3)
            emit(step="4_wait_ready", ok=False, target=target, waited_s=waited,
                 attempts=attempts, reason=last_reason)
            return {
                "ok": False,
                "target": target,
                "source_region": source,
                "failed_step": "4_wait_ready",
                "error": last_reason,
                "state_before": before,
                "snapshot": restored,
                "rpo": rpo,
                "waited_s": waited,
            }
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    waited = round(time.monotonic() - started, 3)
    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited,
         attempts=attempts, ready=ready_body)

    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    previous_region = active_file.read_text().strip() if active_file.exists() else source
    active_file.write_text(target, encoding="utf-8")
    cutover = emit(step="5_dns_cutover", ok=True, target=target,
                   from_region=previous_region, to_region=target)

    try:
        target_state = state_of(target)
    except httpx.HTTPError:
        target_state = ready_body or {"region": target, "ready": True}
    return {
        "ok": True,
        "target": target,
        "source_region": source,
        "state_before": before,
        "target_state": target_state,
        "snapshot": restored,
        "rpo": rpo,
        "waited_s": waited,
        "cutover": cutover,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
