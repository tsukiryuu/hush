"""Background stream (100 tokens = 20s) is running; a priority request must finish fast and cut it off."""
import os, subprocess, sys, time, threading, httpx
H = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(H)
env = {**os.environ, "HUSH_UPSTREAM": "http://127.0.0.1:18091", "HUSH_PORT": "18090"}
up = subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_upstream:app", "--port", "18091", "--log-level", "warning"], cwd=H)
hush = subprocess.Popen([sys.executable, os.path.join(ROOT, "hush.py")], env=env)
try:
    time.sleep(3)
    got = {"bg_chunks": 0}
    def background():
        with httpx.stream("POST", "http://127.0.0.1:18090/v1/chat/completions", json={"stream": True, "max_tokens": 100}, timeout=60) as r:
            for line in r.iter_lines():
                if line.startswith("data: {"): got["bg_chunks"] += 1
    t = threading.Thread(target=background); t.start(); time.sleep(2)
    t0 = time.time()
    r = httpx.post("http://127.0.0.1:18090/v1/chat/completions", json={"max_tokens": 5}, headers={"X-Priority": "1"}, timeout=60)
    dt = time.time() - t0; t.join(timeout=30)
    print(f"priority status={r.status_code} took={dt:.1f}s background_chunks={got['bg_chunks']}/100")
    assert r.status_code == 200 and dt < 5, "priority request should not wait behind background work"
    assert got["bg_chunks"] < 100, "background stream should have been preempted"
    print("PASS")
finally:
    hush.terminate(); up.terminate()
