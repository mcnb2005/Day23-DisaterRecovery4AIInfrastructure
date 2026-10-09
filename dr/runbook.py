"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import math
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step to the incident timeline."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator acknowledgement unless CI uses --auto."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _last_outage(primary: str):
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    latest = None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            latest = event
    return latest


def _primary_ready(primary: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[primary]}/readyz", timeout=timeout)
        if response.status_code == 200:
            return True, "ready"
        try:
            reasons = response.json().get("reasons", [])
        except ValueError:
            reasons = []
        return False, f"http_{response.status_code}:{','.join(map(str, reasons))}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__


def _alive(region: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_for_health_alert(region: str, outage_ts, timeout: float = 6.0) -> bool:
    """Let the independent health checker publish its thresholded alert when present."""
    path = pathlib.Path("reports/health-events.jsonl")
    if not path.exists() or outage_ts is None:
        return False
    deadline = time.monotonic() + timeout
    while True:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (event.get("event") == "state_change"
                    and event.get("region") == region
                    and event.get("to") == "UNHEALTHY"
                    and event.get("ts", 0) >= outage_ts):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _golden_signals(target: str, requests: int = 10) -> dict:
    latencies = []
    errors = 0
    served_by = []
    with httpx.Client(timeout=3.0) as client:
        for i in range(requests):
            started = time.perf_counter()
            try:
                response = client.get(f"{URL[target]}/v1/infer",
                                      params={"q": f"golden-signal-{i}"})
                latency_ms = (time.perf_counter() - started) * 1000
                latencies.append(latency_ms)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                served_by.append(body.get("region"))
                if response.status_code != 200:
                    errors += 1
            except httpx.HTTPError:
                latencies.append((time.perf_counter() - started) * 1000)
                errors += 1
    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "requests": requests,
        "successes": requests - errors,
        "error_rate": round(errors / requests, 3),
        "p95_latency_ms": round(ordered[p95_index], 2),
        "served_by": sorted({region for region in served_by if region}),
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, semi-automated regional recovery runbook."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be valid regions")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    started = time.time()
    probe_results = []
    consecutive_failures = 0
    for attempt in range(1, 4):
        ready, reason = _primary_ready(primary)
        probe_results.append({"attempt": attempt, "ready": ready, "reason": reason})
        consecutive_failures = 0 if ready else consecutive_failures + 1
        if attempt < 3:
            # Keep probe starts five seconds apart, matching the health-check policy.
            # This preserves the 15-second detection floor even for fail-fast outages.
            time.sleep(5.0)
    target_alive = _alive(target)
    outage = _last_outage(primary)
    outage_ts = outage.get("ts") if outage else None
    health_alert_seen = _wait_for_health_alert(primary, outage_ts)
    outage_confirmed = consecutive_failures >= 3 and target_alive
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary,
         target=target, consecutive_failures=consecutive_failures,
         target_alive=target_alive, health_alert_seen=health_alert_seen,
         probes=probe_results)
    if not outage_confirmed:
        return {"ok": False, "failed_step": 1, "reason": "outage_not_confirmed",
                "probes": probe_results, "target_alive": target_alive}

    operator_ts = time.time()
    notification_delay = None if outage_ts is None else round(operator_ts - outage_ts, 3)
    step(2, "thong_bao_incident", ok=True, severity="SEV-1",
         primary=primary, target=target, outage_ts=outage_ts,
         operator_ts=operator_ts, notification_delay_s=notification_delay)

    if not confirm(auto, f"Fail over region-{primary} to region-{target}?"):
        step(3, "scale_gpu_pool", ok=False, cancelled=True,
             reason="operator_declined")
        return {"ok": False, "failed_step": 3, "reason": "operator_declined"}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         target=target, failover_called=1,
         failed_step=failover_result.get("failed_step"),
         waited_s=failover_result.get("waited_s"))

    target_state = failover_result.get("target_state") or {}
    rpo = failover_result.get("rpo") or {}
    replica_ok = bool(failover_result.get("ok") and target_state.get("weights")
                      and target_state.get("count", 0) > 0)
    step(4, "verify_state_replica", ok=replica_ok, target=target,
         vector_count=target_state.get("count"), weights=target_state.get("weights"),
         embed_model_version=(failover_result.get("snapshot") or {}).get("embed_model_version"),
         rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"))

    cutover = failover_result.get("cutover") or {}
    cutover_ok = bool(failover_result.get("ok") and cutover.get("to_region") == target)
    step(5, "dns_cutover", ok=cutover_ok, target=target,
         from_region=cutover.get("from_region"), to_region=cutover.get("to_region"),
         cutover_ts=cutover.get("ts"))
    if not failover_result.get("ok"):
        return {"ok": False, "failed_step": failover_result.get("failed_step"),
                "failover": failover_result}

    golden = _golden_signals(target)
    golden_ok = golden["error_rate"] == 0 and golden["served_by"] == [target]
    step(6, "verify_golden_signals", ok=golden_ok, target=target, **golden)

    elapsed = round(time.time() - started, 3)
    measure_command = ("python tools/measure_rto.py --loadgen "
                       "reports/drill-2-withdr.jsonl --target-rto 300")
    step(7, "post_incident", ok=golden_ok and replica_ok and cutover_ok,
         elapsed_s=elapsed, measure_rto_command=measure_command)
    return {
        "ok": golden_ok and replica_ok and cutover_ok,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed,
        "failover": failover_result,
        "golden_signals": golden,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
