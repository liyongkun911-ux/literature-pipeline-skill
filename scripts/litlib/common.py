"""Shared primitives: run state, HTTP with Retry-After backoff, normalization, JSONL IO."""
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PROFILE = os.path.join(SKILL_ROOT, "config", "profile.default.json")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg, code=1):
    print(f"ERROR {msg}")
    sys.exit(code)


def ok(msg):
    print(f"OK {msg}")


# ---------------------------------------------------------------- run state
class Run:
    def __init__(self, run_id, base=None):
        self.run_id = run_id
        self.dir = os.path.join(base or os.getcwd(), "runs", run_id)
        if not os.path.isdir(self.dir):
            die(f"run not found: {self.dir} (run `lit.py init --topic ...` first)")
        self.profile = json.load(open(os.path.join(self.dir, "profile.json"), encoding="utf-8"))

    def path(self, *parts):
        p = os.path.join(self.dir, *parts)
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        return p

    def has(self, name):
        return os.path.isfile(self.path(name))

    def state(self):
        f = self.path("state.json")
        if os.path.isfile(f):
            return json.load(open(f, encoding="utf-8"))
        return {"run_id": self.run_id, "stages": {}}

    def save_state(self, stage, **kv):
        f = self.path("state.json")
        st = json.load(open(f, encoding="utf-8")) if os.path.isfile(f) else {"run_id": self.run_id, "stages": {}}
        s = st.setdefault("stages", {}).setdefault(stage, {})
        s.update(kv)
        s["at"] = now()
        json.dump(st, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return st

    def add_provenance(self, source, **kv):
        f = self.path("00_provenance.json")
        d = json.load(open(f, encoding="utf-8")) if os.path.isfile(f) else {"source_calls": [], "topic": self.profile.get("topic")}
        d["source_calls"].append(dict(source=source, at=now(), **kv))
        json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- jsonl
def read_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


# ---------------------------------------------------------------- normalization
_WORDS = re.compile(r"[^\w]+")
_ROMAN = re.compile(r"\b(mii|iv|vi|ix|xi|xx)\b", re.I)


def norm_title(t):
    """Case/punctuation/diacritic-insensitive key for fuzzy title matching."""
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", str(t))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("&", " and ").replace("-", " ").replace("’", "'")
    t = _ROMAN.sub(lambda m: m.group(0).lower(), t)
    return " ".join(_WORDS.split(t)).strip()


def norm_doi(d):
    if not d:
        return ""
    d = str(d).strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = d.strip(" .;").replace("doi:", "")
    return d if "/" in d else ""


def norm_arxiv(a):
    if not a:
        return ""
    a = str(a).strip().lower()
    a = re.sub(r"^arxiv:\s*", "", a)
    a = re.sub(r"^https?://arxiv\.org/(abs|pdf|doi)/", "", a)
    a = re.sub(r"v\d+$", "", a)
    return a.strip()


def title_bigrams(tn):
    ws = tn.split()
    return {tuple(ws[i:i + 2]) for i in range(len(ws) - 1)} if len(ws) > 1 else {tuple(ws)} if ws else set()


def title_sim(a, b):
    """Jaccard over word bigrams; robust to subtitle truncation and rewording."""
    A, B = title_bigrams(a), title_bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def tokens(text):
    return [w for w in _WORDS.split((text or "").lower()) if len(w) > 2]


# ---------------------------------------------------------------- HTTP
_HOST_LAST = {}


def _throttle(host, min_interval):
    wait = _HOST_LAST.get(host, 0) - time.time()
    if wait > 0:
        time.sleep(wait)
    _HOST_LAST[host] = max(time.time(), _HOST_LAST.get(host, 0)) + min_interval


def http_get(url, profile=None, headers=None, timeout=30, method="GET", body=None, ct=None, tries=None):
    """Returns (status, bytes, err). Retries on 429/5xx honouring Retry-After; never raises."""
    http = (profile or {}).get("http", {})
    tries = tries if tries is not None else int(http.get("retries", 4))
    cap = float(http.get("backoff_cap_s", 60))
    base = float(http.get("backoff_base_s", 2))
    host = urllib.parse.urlsplit(url).netloc
    hdr = {"User-Agent": f"literature-pipeline/1.0 (mailto:{http.get('polite_mailto','n/a')})",
           "Accept": "application/json, */*"}
    if ct:
        hdr["Content-Type"] = ct
    hdr.update(headers or {})
    err = None
    for i in range(tries):
        if http.get("respect_retry_after", True):
            _throttle(host, float(http.get("per_host_min_interval_s", 1.2)))
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        elif isinstance(body, str):
            data = body.encode()
        elif body is not None:
            data = body
        req = urllib.request.Request(url, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), None
        except urllib.error.HTTPError as e:
            code = e.code
            payload = b""
            try:
                payload = e.read()
            except Exception:
                pass
            err = f"HTTP {code}: {payload[:160].decode('utf8','replace')}"
            if code in (429, 500, 502, 503, 504) and i < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                delay = min(cap, float(ra)) if (ra and str(ra).isdigit()) else min(cap, base * (2 ** i))
                time.sleep(delay)
                continue
            return code, payload, err
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if i < tries - 1:
                time.sleep(min(cap, base * (2 ** i)))
                continue
            return None, b"", err
    return None, b"", err or "exhausted retries"


def http_stream(url, dest, profile=None, max_bytes=64 * 1024 * 1024, headers=None, timeout=90,
                magic=b"%PDF", tries=None):
    """Stream a body to `dest` without holding it in RAM; aborts over max_bytes or non-PDF.

    Transient server-side failures (429/5xx) and truncated transfers (missing %%EOF tail) are
    retried with Retry-After/exponential backoff before giving up, same policy as http_get.
    Returns (status, bytes_written, err). Partial
    files are never left behind, because a truncated .pdf downstream reads exactly like a real
    full text.
    """
    http = (profile or {}).get("http", {})
    tries = tries if tries is not None else int(http.get("retries", 4))
    cap = float(http.get("backoff_cap_s", 60))
    base = float(http.get("backoff_base_s", 2))
    host = urllib.parse.urlsplit(url).netloc
    _throttle(host, float(http.get("per_host_min_interval_s", 1.2)))
    hdr = {"User-Agent": f"literature-pipeline/1.0 (mailto:{http.get('polite_mailto','n/a')})",
           "Accept": "application/pdf, */*"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, headers=hdr)
    tmp = dest + ".part"
    got = 0
    st = None
    reject = None
    for i in range(tries):
        got = 0
        st = None
        reject = None
        fh = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                st = r.status
                fh = open(tmp, "wb")
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    if got == 0 and magic and not chunk.startswith(magic):
                        reject = f"非 PDF({chunk[:12]!r})"
                        break
                    got += len(chunk)
                    if got > max_bytes:
                        reject = f"超过 {max_bytes // (1024 * 1024)}MB"
                        break
                    fh.write(chunk)
        except urllib.error.HTTPError as e:
            st, reject = e.code, f"HTTP {e.code}"
        except Exception as e:
            reject = f"{type(e).__name__}: {e}"
        finally:
            if fh:
                fh.close()
        # 服务器"干净"地提前关连接时 read() 正常返回空、无异常——必须靠尾标记才能识别截断
        # （实测 2026-09-23 run 两篇 arXiv PDF 分别断在整 1MB/3MB，Zotero 打开白屏）
        if not reject and magic == b"%PDF":
            try:
                with open(tmp, "rb") as tf:
                    tf.seek(max(0, got - 1024))
                    if not tf.read().rstrip().endswith(b"%%EOF"):
                        reject = "截断(缺%%EOF)"
            except OSError as e:
                reject = f"{type(e).__name__}: {e}"
        if reject and (st in (429, 500, 502, 503, 504) or reject == "截断(缺%%EOF)") and i < tries - 1:
            time.sleep(min(cap, base * (2 ** i)))
            continue
        break
    if not reject and got == 0:
        reject = "空响应"
    if reject:
        # Windows refuses to unlink a still-open file, so this runs after the close above.
        try:
            os.remove(tmp)
        except OSError:
            pass
        return st, 0, reject
    os.replace(tmp, dest)
    return st, got, None


def get_json(url, profile=None, **kw):
    st, body, err = http_get(url, profile=profile, **kw)
    if err:
        return None, err
    try:
        return json.loads(body.decode("utf8", "replace")), None
    except Exception as e:
        return None, f"bad json: {e}"


# ---------------------------------------------------------------- misc
def pct_rank(value, sorted_values):
    """0..1 percentile of value within a run-local distribution (avoids cross-field bias)."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo / max(1, n - 1)


def slugify(s, n=40):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (s or "run").strip()).strip("-").lower()
    return (s[:n] or "run")


def year_of(rec):
    y = rec.get("year")
    try:
        return int(y)
    except (TypeError, ValueError):
        m = re.search(r"(19|20)\d{2}", str(rec.get("publication_date") or ""))
        return int(m.group(0)) if m else None


def fmt_row(rec, width=62):
    t = (rec.get("title") or "")[:width]
    return f"{rec.get('uid','?'):>7} {str(rec.get('year') or '----'):<5} {t}"


def ensure_io():
    if not isinstance(sys.stdout, io.TextIOWrapper):
        return
