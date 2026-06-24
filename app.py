import os, sys, uuid, json, random, threading, traceback, time, requests
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    parse_session_json, generate_payment_link, PAYMENT_MODES,
    PAY_LONG_LINK_TIMEOUT, ProxyChainServer,
    normalize_proxy_url, mask_proxy_url, randomize_proxy_sid,
)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

DEFAULT_MODE = "PayPal 长链接 US/USD"

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

DEFAULT_GENERATE_RETRIES = _env_int("GENERATE_RETRIES", 20)
MAX_GENERATE_RETRIES = _env_int("MAX_GENERATE_RETRIES", 100)
RETRY_DELAY_SECONDS = _env_float("GENERATE_RETRY_DELAY", 0.5)

# ---- proxy resolution ----

def _pool_from_text(text: str) -> list[str]:
    if not text: return []
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines

def _pool_from_file(path: str) -> list[str]:
    if not path: return []
    p = Path(path)
    if not p.exists(): return []
    return _pool_from_text(p.read_text(encoding="utf-8"))

def _pick_proxy(pool: list[str]) -> str:
    return normalize_proxy_url(random.choice(pool)) if pool else ""

def _retry_count_from_request(data: dict) -> int:
    raw = data.get("retry_count", data.get("retries", DEFAULT_GENERATE_RETRIES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_GENERATE_RETRIES
    return max(1, min(value, MAX_GENERATE_RETRIES))

def _build_effective_proxy(local: str, proxy: str, payment_proxy: str) -> tuple[str, str]:
    """Return (effective_url, label).

    Priority: payment_proxy (direct) > proxy (chained with local) > local > 直连
    支付代理池指定时直接使用，不再经过本地代理链，避免本地代理不可达导致失败。
    """
    local = normalize_proxy_url(local)
    proxy = normalize_proxy_url(proxy)
    payment_proxy = normalize_proxy_url(payment_proxy)
    if proxy: proxy = randomize_proxy_sid(proxy)
    if payment_proxy: payment_proxy = randomize_proxy_sid(payment_proxy)

    # 支付代理优先直接使用，不走本地链
    if payment_proxy:
        return payment_proxy, mask_proxy_url(payment_proxy)

    effective = proxy or local
    if not effective:
        return "", "直连"
    try:
        with ProxyChainServer(local, proxy or "", lambda _: None) as chain:
            return chain.url or effective, mask_proxy_url(effective)
    except Exception:
        return effective, mask_proxy_url(effective)

class PaymentLinkRetryError(RuntimeError):
    def __init__(self, attempts: int, errors: list[dict]):
        self.attempts = attempts
        self.errors = errors
        last_error = errors[-1]["error"] if errors else "未知错误"
        super().__init__(f"生成支付长链接失败，已自动重试 {attempts} 次；最后错误: {last_error}")

def _generate_payment_link_with_retry(access_token: str, mode_name: str, data: dict) -> tuple[dict, dict]:
    retry_count = _retry_count_from_request(data)
    local = data.get("local_proxy", "")
    proxy = data.get("proxy", "")
    payment_proxy = data.get("payment_proxy", "")
    proxy_pool = _pool_from_text(data.get("proxy_pool", ""))
    payment_proxy_pool = _pool_from_text(data.get("payment_proxy_pool", ""))
    errors: list[dict] = []

    for attempt in range(1, retry_count + 1):
        attempt_proxy = proxy or _pick_proxy(proxy_pool)
        attempt_payment_proxy = payment_proxy or _pick_proxy(payment_proxy_pool)
        effective, label = _build_effective_proxy(local, attempt_proxy, attempt_payment_proxy)
        try:
            result = generate_payment_link(access_token, mode_name, effective)
            return result, {
                "attempts": attempt,
                "retry_count": retry_count,
                "proxy_used": label,
                "proxy_url": mask_proxy_url(effective) if effective else "",
                "attempt_errors": errors,
            }
        except Exception as exc:
            errors.append({
                "attempt": attempt,
                "proxy_used": label,
                "error": str(exc),
            })
            if attempt < retry_count and RETRY_DELAY_SECONDS > 0:
                time.sleep(RETRY_DELAY_SECONDS)

    raise PaymentLinkRetryError(retry_count, errors)

def _stream_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"

def _iter_payment_link_events(access_token: str, mode_name: str, data: dict):
    retry_count = _retry_count_from_request(data)
    local = data.get("local_proxy", "")
    proxy = data.get("proxy", "")
    payment_proxy = data.get("payment_proxy", "")
    proxy_pool = _pool_from_text(data.get("proxy_pool", ""))
    payment_proxy_pool = _pool_from_text(data.get("payment_proxy_pool", ""))
    errors: list[dict] = []

    yield {
        "type": "start",
        "ok": True,
        "retry_count": retry_count,
    }

    for attempt in range(1, retry_count + 1):
        attempt_proxy = proxy or _pick_proxy(proxy_pool)
        attempt_payment_proxy = payment_proxy or _pick_proxy(payment_proxy_pool)
        effective, label = _build_effective_proxy(local, attempt_proxy, attempt_payment_proxy)
        try:
            result = generate_payment_link(access_token, mode_name, effective)
            long_url = result.get("long_url") or ""
            yield {
                "type": "success",
                "ok": True,
                "access_token": access_token,
                "long_url": long_url,
                "payment_mode": mode_name,
                "proxy_used": label,
                "proxy_url": mask_proxy_url(effective) if effective else "",
                "attempts": attempt,
                "retry_count": retry_count,
                "attempt_errors": errors,
                "result": {k: v for k, v in result.items()},
            }
            return
        except Exception as exc:
            error = {
                "attempt": attempt,
                "proxy_used": label,
                "error": str(exc),
            }
            errors.append(error)
            yield {
                "type": "attempt_error",
                "ok": False,
                "attempts": attempt,
                "retry_count": retry_count,
                **error,
            }
            if attempt < retry_count and RETRY_DELAY_SECONDS > 0:
                time.sleep(RETRY_DELAY_SECONDS)

    last_error = errors[-1]["error"] if errors else "未知错误"
    yield {
        "type": "error",
        "ok": False,
        "error": f"生成支付长链接失败，已自动重试 {retry_count} 次；最后错误: {last_error}",
        "attempts": retry_count,
        "retry_count": retry_count,
        "attempt_errors": errors,
    }

# ---- routes ----

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat() + "Z"})

@app.route("/api/payment-modes")
def payment_modes():
    modes = {}
    for name, cfg in PAYMENT_MODES.items():
        modes[name] = {
            "country": cfg.get("country", ""),
            "currency": cfg.get("currency", ""),
            "paypal": "PayPal" in name,
        }
    return jsonify({"ok": True, "modes": modes})

@app.route("/api/generate-link", methods=["POST"])
def generate_link():
    data = request.get_json(silent=True) or {}
    access_token = (data.get("access_token") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()

    if not access_token:
        return jsonify({"ok": False, "error": "缺少 access_token"}), 400

    try:
        result, meta = _generate_payment_link_with_retry(access_token, mode_name, data)
        long_url = result.get("long_url") or ""
        return jsonify({
            "ok": True, "long_url": long_url, "payment_mode": mode_name,
            "proxy_used": meta["proxy_used"], "proxy_url": meta["proxy_url"],
            "attempts": meta["attempts"], "retry_count": meta["retry_count"],
            "attempt_errors": meta["attempt_errors"],
            "result": {k: v for k, v in result.items()},
        })
    except PaymentLinkRetryError as exc:
        return jsonify({
            "ok": False, "error": str(exc), "attempts": exc.attempts,
            "attempt_errors": exc.errors,
        }), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/paste-session", methods=["POST"])
def paste_session():
    data = request.get_json(silent=True) or {}
    session_text = (data.get("session_json") or data.get("session_text") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()

    if not session_text:
        return jsonify({"ok": False, "error": "缺少 session_json / session_text"}), 400

    access_token = parse_session_json(session_text)
    if not access_token:
        return jsonify({"ok": False, "error": "未能从粘贴内容解析 accessToken"}), 400

    try:
        result, meta = _generate_payment_link_with_retry(access_token, mode_name, data)
        long_url = result.get("long_url") or ""
        return jsonify({
            "ok": True, "access_token": access_token, "long_url": long_url,
            "payment_mode": mode_name, "proxy_used": meta["proxy_used"],
            "attempts": meta["attempts"], "retry_count": meta["retry_count"],
            "attempt_errors": meta["attempt_errors"],
            "result": {k: v for k, v in result.items()},
        })
    except PaymentLinkRetryError as exc:
        return jsonify({
            "ok": False, "error": str(exc), "attempts": exc.attempts,
            "attempt_errors": exc.errors,
        }), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/paste-session-stream", methods=["POST"])
def paste_session_stream():
    data = request.get_json(silent=True) or {}
    session_text = (data.get("session_json") or data.get("session_text") or "").strip()
    mode_name = (data.get("payment_mode") or DEFAULT_MODE).strip()

    if not session_text:
        return jsonify({"ok": False, "error": "缺少 session_json / session_text"}), 400

    access_token = parse_session_json(session_text)
    if not access_token:
        return jsonify({"ok": False, "error": "未能从粘贴内容解析 accessToken"}), 400

    def generate():
        for event in _iter_payment_link_events(access_token, mode_name, data):
            yield _stream_json(event)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.route("/api/proxy-test", methods=["POST"])
def proxy_test():
    """Test a proxy URL by requesting ipinfo.io."""
    data = request.get_json(silent=True) or {}
    url = normalize_proxy_url(data.get("proxy_url", ""))
    if not url:
        return jsonify({"ok": False, "error": "缺少 proxy_url"}), 400
    try:
        r = requests.get("https://ipinfo.io/json",
                         proxies={"http": url, "https": url},
                         timeout=15)
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}"})
        p = r.json() or {}
        return jsonify({
            "ok": True,
            "ip": p.get("ip", ""),
            "country": p.get("country", ""),
            "city": p.get("city", ""),
            "org": p.get("org", ""),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"))
