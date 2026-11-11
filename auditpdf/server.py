"""`python -m auditpdf serve` - 이 PC(127.0.0.1)에서만 열리는 뷰어 서버.

PDF 는 수백 건이면 읽는 데 몇 분 걸리므로 별도 스레드에서 읽고, 화면은 /api/status 로 진행률을 받다가
다 읽으면 새로 고친다. 읽은 결과는 메모리에 두고, 같은 파일은 다시 열지 않는다(scan.py).
PDF 루트 폴더는 화면의 '폴더 설정'에서 바꿀 수 있고 설정 파일에 저장된다. 원본 PDF 는 읽기만 한다.
"""

import gc
import gzip
import io
import json
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from functools import lru_cache

from .config import CONFIG_PATH, browse_folder, check_root, load_config, save_root
from .images import ng_image

LAZY = ("bottle", "ng")
from .scan import Cancelled, scan


def serve(args, build_data, render):
    lock = threading.Lock()
    browse_lock = threading.Lock()
    st = {"root": args.root, "html": None, "result": None, "rows": {}, "gen": 0,
          "job": None}  # job: {"done", "total", "name", "cancel", "error"}

    def settings():
        return {"root": st["root"], "recent": load_config().get("recent", []), "config": str(CONFIG_PATH)}

    def empty_page(error=None, job=False):
        return render({
            "generated": "", "source": st["root"], "live": True, "files": [], "audit": [], "meta_keys": [],
            "error": error, "job": job, "settings": settings(),
        }).encode("utf-8")

    def start(root):
        """root 를 새로 읽기 시작한다. lock 안에서 부른다."""
        if st["job"]:
            st["job"]["cancel"].set()
        st["root"], st["gen"] = str(root), st["gen"] + 1
        st["html"], st["result"], st["rows"] = None, None, {}
        error = check_root(root)
        if error:
            st["job"] = None
            st["html"] = empty_page(error)
            return error
        gen, cancel = st["gen"], threading.Event()
        job = st["job"] = {"done": 0, "total": 0, "name": "", "cancel": cancel, "error": None}

        def progress(done, total, name):
            job.update(done=done, total=total, name=name)

        def run():
            print(f"PDF 를 읽는 중... {root}")
            try:
                result = scan(root, progress, cancel)
                data = build_data(result, live=True)
                # 병 계수 기록과 NG 이미지 목록은 수십만 행이라 화면에 넣지 않고, 그 탭을 처음 열 때 /api/rows 로 받는다
                rows = {k: gzip.compress(json.dumps(data.pop(k), ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
                        for k in LAZY}
                data["lazy"] = True
                html = render({**data, "settings": settings()}).encode("utf-8")
                del data
                error = None
            except Cancelled:
                return
            except Exception as e:  # 서버는 살려 두고 화면에 이유를 보여 준다
                traceback.print_exc()
                result, rows, error = None, {}, f"PDF 를 읽지 못했습니다: {e}"
                html = empty_page(error)
            gc.collect()
            with lock:
                if st["gen"] != gen:  # 읽는 동안 폴더가 바뀌었다
                    return
                st["result"], st["html"], st["rows"], st["job"] = result, html, rows, None
                if result is not None:
                    print(f"읽기 완료: PDF {len(result.files)}건, 로그 {len(result.records):,}행")
                job["error"] = error

        threading.Thread(target=run, name="auditpdf-scan", daemon=True).start()
        return None

    def page(reload=False):
        with lock:
            if reload or (st["html"] is None and st["job"] is None):
                start(st["root"])
            if st["html"] is not None:
                return st["html"]
            return empty_page(job=True)

    def status():
        with lock:
            job = st["job"]
            if job is None:
                return {"running": False}
            return {"running": True, "done": job["done"], "total": job["total"], "name": job["name"]}

    def excel(per_folder):
        from .excel import write_excel

        with lock:
            result = st["result"]
        if result is None:
            return None
        buf = io.BytesIO()
        write_excel(result, buf, per_folder=per_folder)
        return buf.getvalue()

    @lru_cache(maxsize=512)
    def image(path, mtime, page, index):
        """NG 이미지. PDF 를 매번 여는 대신 최근 것은 메모리에 둔다 (mtime 이 바뀌면 다시 읽는다)."""
        return ng_image(path, mtime, page, index)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, ctype, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not any(k == "Cache-Control" for k, _ in headers):
                self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, ctype, name):
            """큰 파일(수백 MB)도 메모리에 다 올리지 않고 나눠 보낸다."""
            size = Path(path).stat().st_size
            with open(path, "rb") as fh:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(name)}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while chunk := fh.read(1 << 20):
                        self.wfile.write(chunk)
                except (ConnectionError, OSError):
                    pass  # 받는 중에 탭을 닫음

        def send_json(self, status, obj):
            self.send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send(200, page(reload="reload" in q), "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    self.send_json(200, status())
                elif url.path == "/api/rows":
                    with lock:
                        body = st["rows"].get(q.get("kind", [""])[0])
                    if body is None:
                        return self.send(404, b"[]", "application/json")
                    self.send(200, body, "application/json; charset=utf-8", [("Content-Encoding", "gzip")])
                elif url.path == "/pdf":
                    # 원본 PDF 를 브라우저에서 연다. 읽은 파일 목록에 있는 것만 (번호로) 내보낸다
                    with lock:
                        result = st["result"]
                    f = result.files[int(q["f"][0])] if result else None
                    if f is None:
                        return self.send(404, b"", "text/plain")
                    self.send_file(f.path, "application/pdf", f.name)
                elif url.path == "/img":
                    with lock:
                        result = st["result"]
                    f = result.files[int(q["f"][0])] if result else None
                    if f is None or f.kind != "ng" or f.data is None:
                        return self.send(404, b"", "text/plain")
                    g = f.data.ngs[int(q["n"][0])]
                    body, ctype = image(f.path, f.mtime, g[3], g[4])
                    self.send(200, body, ctype, [("Cache-Control", "max-age=3600")])
                elif url.path == "/api/export.xlsx":
                    body = excel(q.get("per_folder", ["0"])[0] == "1")
                    if body is None:
                        return self.send(409, "아직 읽은 데이터가 없습니다.".encode("utf-8"), "text/plain; charset=utf-8")
                    name = f"{Path(st['root']).name or 'audit_trail'}_audit_trail.xlsx"
                    self.send(200, body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              [("Content-Disposition",
                                f"attachment; filename=\"audit_trail_merged.xlsx\"; filename*=UTF-8''{quote(name)}")])
                else:
                    self.send(404, b"not found", "text/plain")
            except Exception as e:  # 잘못된 번호, 손상된 PDF 등: 요청 하나만 실패시킨다
                self.send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

        def do_POST(self):
            # 설정 변경은 이 화면에서 보낸 JSON 요청만 받는다 (다른 사이트가 보낸 폼 요청 차단)
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if (not self.headers.get("Content-Type", "").startswith("application/json")
                    or (origin and urlparse(origin).netloc != host)):
                return self.send_json(403, {"error": "허용되지 않은 요청"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "잘못된 요청"})
            url = urlparse(self.path)
            if url.path == "/api/root":
                root = str(body.get("root", "")).strip().strip('"')
                if not root:
                    return self.send_json(400, {"error": "폴더 경로를 입력하세요."})
                root = str(Path(root).expanduser())
                error = check_root(root)
                if error:
                    return self.send_json(400, {"error": error})
                save_root(root)
                with lock:
                    start(root)
                print(f"PDF 루트 폴더 변경: {root}")
                self.send_json(200, {"root": root})
            elif url.path == "/api/cancel":
                with lock:
                    if st["job"]:
                        st["job"]["cancel"].set()
                        st["job"], st["gen"] = None, st["gen"] + 1
                        st["html"] = empty_page("읽기를 중지했습니다. '다시 읽기'로 처음부터 읽을 수 있습니다.")
                self.send_json(200, {})
            elif url.path == "/api/browse":
                if not browse_lock.acquire(blocking=False):
                    return self.send_json(409, {"error": "폴더 선택 창이 이미 열려 있습니다."})
                try:
                    self.send_json(200, {"root": browse_folder(body.get("initial"))})
                except Exception as e:  # tkinter 가 없는 Python 등
                    self.send_json(500, {"error": f"폴더 선택 창을 열 수 없습니다: {e}"})
                finally:
                    browse_lock.release()
            else:
                self.send_json(404, {"error": "not found"})

        def log_message(self, fmt, *a):
            pass

    page()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"뷰어 실행 중: {url}  (종료: Ctrl+C)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
