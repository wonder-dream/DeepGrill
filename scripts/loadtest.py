"""本地真并发压测：只读接口 + 受限写接口（真实 LLM 慎压）。

跑法：
    uv run python scripts/loadtest.py --url http://127.0.0.1:8000 --token <token> --mode read --concurrency 20 --total 200
    uv run python scripts/loadtest.py --url http://127.0.0.1:8000 --token <token> --mode answer --concurrency 2 --total 4

- read：并发打 /api/today /api/bank /api/history /api/review，统计 P50/P95/错误率
- answer：并发答题（真实 DeepSeek 调用，烧钱且慢；并发必须 ≤2，否则打满 uvicorn 线程池）
  answer 需先手动构造一个待答题的 session：--question-id 与 --answer 参数
- 观察点：错误率应为 0；database is locked 不应出现；P95 读接口 < 500ms（本地）
"""
import argparse
import random
import statistics
import threading
import time

import httpx

READ_PATHS = [
    "/api/today",
    "/api/bank?page=1&page_size=20",
    "/api/history",
    "/api/review/tags",
]


def run_read(client: httpx.Client, concurrency: int, total: int) -> tuple[list[float], list[str]]:
    timings: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker():
        for _ in range(total):
            path = random.choice(READ_PATHS)
            t0 = time.perf_counter()
            try:
                r = client.get(path)
                dt = (time.perf_counter() - t0) * 1000
                with lock:
                    timings.append(dt)
                if r.status_code != 200:
                    with lock:
                        errors.append(f"{path} -> {r.status_code}")
            except Exception as e:
                with lock:
                    errors.append(f"{path} -> {e}")

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return timings, errors


def run_answer(
    client: httpx.Client, concurrency: int, total: int, question_id: int, answer: str
) -> None:
    """创建会话 → 提交回答 → 轮询到终态（done/failed/超时）。"""
    timings: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker():
        for i in range(total):
            t0 = time.perf_counter()
            try:
                created = client.post(
                    "/api/sessions",
                    json={"question_id": question_id, "kind": "open"},
                )
                if created.status_code not in (200, 201):
                    with lock:
                        errors.append(f"create session -> {created.status_code}")
                    return
                sid = created.json()["session_id"]
                r = client.post(
                    f"/api/sessions/{sid}/answer", json={"answer": answer}
                )
                if r.status_code not in (200, 201):
                    with lock:
                        errors.append(f"answer -> {r.status_code}: {r.text[:100]}")
                    return
                for _ in range(180):  # 轮询上限 3 分钟
                    s = client.get(f"/api/sessions/{sid}").json()
                    if s["status"] in ("done", "failed"):
                        dt = (time.perf_counter() - t0) * 1000
                        with lock:
                            timings.append(dt)
                            if s["status"] == "failed":
                                errors.append("judge failed")
                        return
                    time.sleep(1)
                with lock:
                    errors.append("poll timeout")
            except Exception as e:
                with lock:
                    errors.append(f"answer flow -> {e}")

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"[answer] 完成 {len(timings)}/{total * concurrency}，耗时 ms: "
          f"P50={statistics.median(timings) if timings else 0:.0f} "
          f"P95={sorted(timings)[int(len(timings) * 0.95)] if timings else 0:.0f}")
    if errors:
        print(f"[answer] 错误 {len(errors)} 条（前 5）：")
        for e in errors[:5]:
            print("  ", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--mode", choices=["read", "answer"], required=True)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--total", type=int, default=100, help="每个线程的请求数（read）或轮数（answer）")
    parser.add_argument("--question-id", type=int, default=None, help="answer 模式：待答题 id")
    parser.add_argument("--answer", default="我通过双写与队列保证最终一致，先更新数据库再异步同步缓存", help="answer 模式：回答内容")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.url, headers=headers, timeout=30) as client:
        if args.mode == "read":
            timings, errors = run_read(client, args.concurrency, args.total)
            print(f"[read] 请求 {len(timings)}，耗时 ms：P50={statistics.median(timings):.0f} "
                  f"P95={sorted(timings)[int(len(timings) * 0.95)]:.0f} max={max(timings):.0f}")
            print(f"[read] 错误 {len(errors)} 条（前 5）：")
            for e in errors[:5]:
                print("  ", e)
            print("结论：错误率 0 且 P95 < 500ms 即通过")
        else:
            if not args.question_id:
                raise SystemExit("answer 模式必须指定 --question-id")
            run_answer(client, args.concurrency, args.total, args.question_id, args.answer)


if __name__ == "__main__":
    main()
