"""`python -m auditpdf serve` - 뷰어 서버.

PDF 는 수백 건이면 읽는 데 몇 분 걸리므로 별도 스레드에서 읽고, 화면은 /api/status 로 진행률을 받다가
다 읽으면 새로 고친다. 읽은 결과는 장비(PDF 루트 폴더)별로 메모리에 두고, 같은 파일은 다시 열지 않는다(scan.py).
원본 PDF 는 읽기만 한다.

접속
- 기본은 이 PC(127.0.0.1)에서만 열린다. 설정의 lan(화면의 '접속 허용 IP') 또는 --host 0.0.0.0 이면 내부망에 열고,
  허용 IP 목록에 있는 PC 만 받는다. 목록에 없는 PC 는 거절하고 '최근 거부된 접속'에 남긴다.
- 서버 PC 자신은 관리자: 장비 추가·변경·삭제, 다시 읽기, 중지, 접속 허용 IP 를 바꿀 수 있다.
  허용된 다른 PC 는 보기만 한다 (장비 탭, 검색, PDF 원본, NG 이미지, Excel/CSV 저장).
- 보는 장비는 주소(/?d=장비 번호)로 정하므로 사람마다 다른 장비를 동시에 볼 수 있다.
- /wc 는 중량선별기 운영 화면 (보기 전용, 허용 IP 도 열 수 있다). 장비 설정에서 '운영 화면에 표시'한 장비만 보인다.
- /lab 은 서버 PC 에서만 여는 테스트 페이지 (중량선별기 장비 관리와 아직 운영에 올리지 않은 장비, lab.py).
- '서버 다시 시작'(POST /api/restart)은 프로세스는 그대로 두고 접속받는 소켓만 설정의 lan 에 맞춰 다시 연다.
  읽은 결과는 메모리에 그대로 남으므로 PDF 를 다시 읽지 않는다.
"""

import gc
import gzip
import io
import ipaddress
import json
import socket
import threading
import time
import traceback
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .config import (CONFIG_PATH, access, browse_folder, check_root, configured_root, devices, load_config,
                     save_access, save_devices, wc_devices)
from .images import ng_image
from .lab import Lab
from .scan import Cancelled, scan

LAZY = ("bottle", "ng")
SET_PLACEHOLDER = b"/*__SET__*/null"   # 요청마다 넣는 화면 설정 (장비 목록, 관리자 여부 등)
DENIED_MAX = 20


def client_ip(handler):
    ip = handler.client_address[0]
    return ip[7:] if ip.startswith("::ffff:") else ip


def is_local(ip, server_ip):
    """서버 PC 자신에서 온 요청인지 (127.0.0.1 이나 이 PC 의 내부망 주소로 접속)."""
    try:
        return ipaddress.ip_address(ip).is_loopback or ip == server_ip
    except ValueError:
        return False


def parse_rule(rule):
    """허용 IP 한 줄 → ip_network. 192.168.10.21 또는 192.168.10.0/24. 잘못되면 ValueError."""
    return ipaddress.ip_network(rule.strip(), strict=False)


def allowed(ip, rules):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for r in rules:
        try:
            if addr in parse_rule(r["ip"]):
                return True
        except ValueError:
            continue
    return False


def lan_addresses():
    try:
        return sorted({a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")})
    except OSError:
        return []


class View:
    """장비(PDF 루트 폴더) 하나의 읽은 결과."""

    def __init__(self, root):
        self.root = str(root)
        self.html = None      # 다 읽은 화면 (또는 오류 화면)
        self.result = None
        self.rows = {}
        self.rid = 0          # 이 결과의 번호. /pdf, /img, /api/rows 요청에 붙어 온다
        self.job = None       # 읽는 중: {"done", "total", "name", "cancel"}


def serve(args, build_data, render):
    lock = threading.Lock()
    browse_lock = threading.Lock()
    views = {}       # 폴더(소문자) → View
    by_rid = {}      # rid → View
    denied = {}      # IP → {"ip", "last", "count"}: 허용 목록에 없어 거절한 접속
    lab = Lab()
    counter = [0]
    # host: 지금 접속받는 주소. restart: '서버 다시 시작' 요청 (serve_forever 를 멈추고 다시 연다)
    srv = {"host": args.host or ("0.0.0.0" if access()["lan"] else "127.0.0.1"), "httpd": None,
           "restart": False, "error": ""}

    def empty_page(root, error=None, job=False):
        return render({
            "generated": "", "source": root, "live": True, "files": [], "audit": [], "meta_keys": [],
            "error": error, "job": job,
        }).encode("utf-8")

    def device_index(devs, q):
        """요청한 장비 번호. 없으면 마지막으로 설정한 폴더의 장비, 그것도 없으면 첫 장비."""
        try:
            d = int(q.get("d", [""])[0])
            if 0 <= d < len(devs):
                return d
        except ValueError:
            pass
        current = (configured_root() or args.root or "").lower()
        return next((i for i, x in enumerate(devs) if x["root"].lower() == current), 0 if devs else -1)

    def view_of(root):
        """lock 안에서 부른다."""
        key = root.lower()
        if key not in views:
            views[key] = View(root)
        return views[key]

    def drop(root):
        """장비를 지우거나 폴더를 바꿀 때: 읽은 결과와 읽는 중인 작업을 버린다. lock 안에서 부른다."""
        v = views.pop(root.lower(), None)
        if v is None:
            return
        if v.job:
            v.job["cancel"].set()
        by_rid.pop(v.rid, None)

    def start(v):
        """v 를 새로 읽기 시작한다. lock 안에서 부른다."""
        if v.job:
            v.job["cancel"].set()
        by_rid.pop(v.rid, None)
        v.html, v.result, v.rows, v.rid = None, None, {}, 0
        error = check_root(v.root)
        if error:
            v.html = empty_page(v.root, error)
            return
        counter[0] += 1
        rid, cancel = counter[0], threading.Event()
        job = v.job = {"done": 0, "total": 0, "name": "", "cancel": cancel}

        def progress(done, total, name):
            job.update(done=done, total=total, name=name)

        def run():
            print(f"PDF 를 읽는 중... {v.root}")
            try:
                result = scan(v.root, progress, cancel)
                data = build_data(result, live=True)
                # 병 계수 기록과 NG 이미지 목록은 수십만 행이라 화면에 넣지 않고, 그 탭을 처음 열 때 /api/rows 로 받는다
                rows = {k: gzip.compress(json.dumps(data.pop(k), ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
                        for k in LAZY}
                data["lazy"] = True
                html = render({**data, "rid": rid}).encode("utf-8")
                del data
            except Cancelled:
                return
            except Exception as e:  # 서버는 살려 두고 화면에 이유를 보여 준다
                traceback.print_exc()
                result, rows, html = None, {}, empty_page(v.root, f"PDF 를 읽지 못했습니다: {e}")
            gc.collect()
            with lock:
                if v.job is not job:  # 읽는 동안 중지됐거나 다시 읽기 시작했다
                    return
                v.html, v.result, v.rows, v.job = html, result, rows, None
                if result is not None:
                    v.rid = rid
                    by_rid[rid] = v
                    print(f"읽기 완료: {v.root} · PDF {len(result.files)}건, 로그 {len(result.records):,}행")

        threading.Thread(target=run, name="auditpdf-scan", daemon=True).start()

    def page(root, reload):
        """root 장비의 화면. 처음이면(또는 reload) 읽기 시작하고 진행률 화면을 준다."""
        with lock:
            v = view_of(root)
            if reload or (v.html is None and v.job is None):
                start(v)
            return v.html if v.html is not None else empty_page(root, job=True)

    def page_settings(handler, q, admin):
        """화면에 넣는 설정. 폴더 경로와 접속 설정은 관리자에게만."""
        cfg = load_config()
        devs = devices(cfg)
        d = device_index(devs, q)
        s = {"admin": admin, "ip": client_ip(handler), "device": d,
             "devices": [{"name": x["name"], **({"root": x["root"]} if admin else {})} for x in devs],
             "wc": sum(1 for x in wc_devices(cfg) if x["stage"] == "live")}   # 운영 중량선별기 장비 수 (카테고리 탭)
        if admin:
            with lock:
                recent_denied = sorted(denied.values(), key=lambda x: -x["last"])
            s.update(config=str(CONFIG_PATH), recent=cfg.get("recent", []), access=access(cfg),
                     listening=srv["host"], listen_error=srv["error"], port=args.port, lan=lan_addresses(),
                     denied=[{"ip": x["ip"], "count": x["count"],
                              "last": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x["last"]))}
                             for x in recent_denied])
        return s, devs, d

    def excel(result, per_folder):
        from .excel import write_excel

        buf = io.BytesIO()
        write_excel(result, buf, per_folder=per_folder)
        return buf.getvalue()

    @lru_cache(maxsize=512)
    def image(path, mtime, page_no, index):
        """NG 이미지. PDF 를 매번 여는 대신 최근 것은 메모리에 둔다 (mtime 이 바뀌면 다시 읽는다)."""
        return ng_image(path, mtime, page_no, index)

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

        def send_text(self, status, text):
            self.send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

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

        def admin(self):
            return is_local(client_ip(self), self.connection.getsockname()[0])

        def check_access(self):
            """서버 PC 이거나 허용 IP 면 True. 아니면 거절 화면을 보내고 기록한다."""
            if self.admin():
                return True
            ip = client_ip(self)
            if allowed(ip, access()["allow"]):
                return True
            with lock:
                x = denied.setdefault(ip, {"ip": ip, "last": 0, "count": 0})
                x["last"], x["count"] = time.time(), x["count"] + 1
                while len(denied) > DENIED_MAX:
                    denied.pop(min(denied, key=lambda k: denied[k]["last"]))
            body = (f'<!doctype html><meta charset="utf-8"><title>접속 거부</title>'
                    f'<body style="font-family:Malgun Gothic,sans-serif;padding:40px;line-height:1.6">'
                    f'<h2>접속이 허용되지 않은 PC 입니다</h2><p>이 PC 의 IP: <b>{ip}</b></p>'
                    f'<p>서버 PC 의 뷰어에서 \'접속 허용 IP\' 에 이 IP 를 등록해 달라고 관리자에게 요청하세요.</p>')
            self.send(403, body.encode("utf-8"), "text/html; charset=utf-8")
            return False

        def result_of(self, q):
            """rid 로 결과를 찾는다. 다시 읽었거나 장비가 지워져 없으면 None."""
            try:
                rid = int(q.get("r", [""])[0])
            except ValueError:
                return None
            with lock:
                v = by_rid.get(rid)
                return v.result if v else None

        def do_GET(self):
            if not self.check_access():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/wc" or url.path.startswith("/wc/"):   # 중량선별기 운영 화면 (보기 전용)
                    return lab.get(self, url.path, q, live=True, admin=self.admin())
                if url.path == "/lab" or url.path.startswith("/lab/"):
                    if not self.admin():
                        return self.send_text(403, "테스트 페이지는 서버 PC 에서만 열 수 있습니다.")
                    return lab.get(self, url.path, q)
                if url.path == "/":
                    admin = self.admin()
                    s, devs, d = page_settings(self, q, admin)
                    if d < 0:
                        html = empty_page("", "장비가 없습니다. '+ 장비 추가'로 장비 이름과 PDF 루트 폴더를 등록하세요."
                                          if admin else "등록된 장비가 없습니다. 서버 PC 에서 장비를 추가해야 합니다.")
                    else:
                        html = page(devs[d]["root"], reload="reload" in q and admin)
                    js = json.dumps(s, ensure_ascii=False).replace("</", "<\\/").encode("utf-8")
                    self.send(200, html.replace(SET_PLACEHOLDER, js, 1), "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    devs = devices()
                    d = device_index(devs, q)
                    with lock:
                        v = views.get(devs[d]["root"].lower()) if d >= 0 else None
                        job = v.job if v else None
                        self.send_json(200, {"running": False} if job is None else
                                       {"running": True, "done": job["done"], "total": job["total"], "name": job["name"]})
                elif url.path == "/api/rows":
                    try:
                        rid = int(q.get("r", [""])[0])
                    except ValueError:
                        rid = 0
                    with lock:
                        v = by_rid.get(rid)
                        body = v.rows.get(q.get("kind", [""])[0]) if v else None
                    if body is None:
                        return self.send_text(409, "다시 읽었거나 지워진 장비입니다. 화면을 새로 고치세요.")
                    self.send(200, body, "application/json; charset=utf-8", [("Content-Encoding", "gzip")])
                elif url.path in ("/pdf", "/img"):
                    # 원본 PDF / NG 이미지. 읽은 파일 목록에 있는 것만 (번호로) 내보낸다
                    result = self.result_of(q)
                    if result is None:
                        return self.send_text(409, "다시 읽었거나 지워진 장비입니다. 이 창을 닫고 다시 여세요.")
                    f = result.files[int(q["f"][0])]
                    if url.path == "/pdf":
                        return self.send_file(f.path, "application/pdf", f.name)
                    if f.kind != "ng" or f.data is None:
                        return self.send(404, b"", "text/plain")
                    g = f.data.ngs[int(q["n"][0])]
                    body, ctype = image(f.path, f.mtime, g[3], g[4])
                    self.send(200, body, ctype, [("Cache-Control", "max-age=3600")])
                elif url.path == "/api/export.xlsx":
                    result = self.result_of(q)
                    if result is None:
                        return self.send_text(409, "아직 읽은 데이터가 없습니다. 화면을 새로 고치세요.")
                    body = excel(result, q.get("per_folder", ["0"])[0] == "1")
                    name = f"{Path(result.root).name or 'audit_trail'}_audit_trail.xlsx"
                    self.send(200, body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              [("Content-Disposition",
                                f"attachment; filename=\"audit_trail_merged.xlsx\"; filename*=UTF-8''{quote(name)}")])
                else:
                    self.send(404, b"not found", "text/plain")
            except ConnectionError:
                pass  # 받는 중에 창을 닫음
            except FileNotFoundError as e:
                self.send_text(404, f"파일이 없습니다: {e}")
            except Exception as e:  # 잘못된 번호, 손상된 PDF 등: 요청 하나만 실패시킨다
                self.send_text(500, str(e))

        def do_POST(self):
            if not self.check_access():
                return
            # 설정 변경은 서버 PC 의 이 화면에서 보낸 JSON 요청만 받는다 (다른 PC, 다른 사이트가 보낸 폼 요청 차단)
            if not self.admin():
                return self.send_json(403, {"error": "설정은 서버 PC 에서만 바꿀 수 있습니다."})
            host_hdr = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if (not self.headers.get("Content-Type", "").startswith("application/json")
                    or (origin and urlparse(origin).netloc != host_hdr)):
                return self.send_json(403, {"error": "허용되지 않은 요청"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "잘못된 요청"})
            url = urlparse(self.path)
            if url.path.startswith("/lab/"):
                lab.post(self, url.path, body)
            elif url.path == "/api/device":
                self.device(body)
            elif url.path == "/api/access":
                self.save_access(body)
            elif url.path == "/api/cancel":
                devs = devices()
                d = device_index(devs, {"d": [str(body.get("index", ""))]})
                with lock:
                    v = views.get(devs[d]["root"].lower()) if d >= 0 else None
                    if v and v.job:
                        v.job["cancel"].set()
                        v.job = None
                        v.html = empty_page(v.root, "읽기를 중지했습니다. '다시 읽기'로 처음부터 읽을 수 있습니다.")
                self.send_json(200, {})
            elif url.path == "/api/restart":
                # 응답을 보낸 뒤 다른 스레드에서 멈춘다 (shutdown 은 serve_forever 가 끝날 때까지 기다린다)
                srv["restart"] = True
                self.send_json(200, {"host": "0.0.0.0" if access()["lan"] else "127.0.0.1"})
                threading.Thread(target=srv["httpd"].shutdown, daemon=True).start()
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

        def device(self, body):
            """장비 추가(index 없음) / 고치기(index) / 지우기(index + delete). 끝나면 보여 줄 장비 번호를 돌려준다."""
            devs = devices()
            index = body.get("index")
            if index is not None:
                try:
                    index = int(index)
                    devs[index]
                except (TypeError, ValueError, IndexError):
                    return self.send_json(400, {"error": "없는 장비입니다."})
            if body.get("delete"):
                if index is None:
                    return self.send_json(400, {"error": "지울 장비를 고르세요."})
                gone = devs.pop(index)
                if not any(d["root"].lower() == gone["root"].lower() for d in devs):
                    with lock:
                        drop(gone["root"])
                save_devices(devs, devs[0]["root"] if devs else "")
                print(f"장비 삭제: {gone['name']}")
                return self.send_json(200, {"index": 0 if devs else -1})
            name = str(body.get("name", "")).strip()
            root = str(body.get("root", "")).strip().strip('"')
            if not name:
                return self.send_json(400, {"error": "장비 이름을 입력하세요."})
            if not root:
                return self.send_json(400, {"error": "폴더 경로를 입력하세요."})
            root = str(Path(root).expanduser())
            error = check_root(root)
            if error:
                return self.send_json(400, {"error": error})
            for i, d in enumerate(devs):
                if i != index and d["name"] == name:
                    return self.send_json(400, {"error": f"같은 이름의 장비가 있습니다: {name}"})
            if index is None:
                devs.append({"name": name, "root": root})
                index = len(devs) - 1
            else:
                old = devs[index]["root"]
                devs[index] = {"name": name, "root": root}
                if old.lower() != root.lower() and not any(d["root"].lower() == old.lower() for d in devs):
                    with lock:
                        drop(old)
            save_devices(devs, root)
            print(f"장비 저장: {name} → {root}")
            self.send_json(200, {"index": index})

        def save_access(self, body):
            allow, seen = [], set()
            for a in body.get("allow", []):
                ip = str(a.get("ip", "")).strip() if isinstance(a, dict) else ""
                name = str(a.get("name", "")).strip() if isinstance(a, dict) else ""
                if not ip:
                    continue
                try:
                    net = parse_rule(ip)
                except ValueError:
                    return self.send_json(400, {"error": f"IP 형식이 아닙니다: {ip}  (예: 192.168.10.21 또는 192.168.10.0/24)"})
                if net.num_addresses > 256:
                    return self.send_json(400, {"error": f"대역이 너무 넓습니다: {ip}  (/24 보다 좁게)"})
                if str(net) in seen:
                    return self.send_json(400, {"error": f"같은 IP 가 두 번 있습니다: {ip}"})
                seen.add(str(net))
                allow.append({"ip": ip, "name": name})
            save_access(body.get("lan"), allow)
            with lock:  # 허용한 IP 는 거부 기록에서 뺀다
                for k in [k for k in denied if allowed(k, allow)]:
                    denied.pop(k)
            print(f"접속 허용 IP 저장: {', '.join(a['ip'] for a in allow) or '(없음)'} · 다른 PC 접속 {'켜짐' if body.get('lan') else '꺼짐'}")
            self.send_json(200, {"restart": bool(body.get("lan")) != (srv["host"] != "127.0.0.1")})

        def log_message(self, fmt, *a):
            pass

    def listen(host):
        srv["httpd"], srv["host"] = ThreadingHTTPServer((host, args.port), Handler), host
        print(f"뷰어 실행 중: http://127.0.0.1:{args.port}/  (종료: Ctrl+C)")
        if host != "127.0.0.1":
            rules = access()["allow"]
            print(f"다른 PC 접속: " + ", ".join(f"http://{a}:{args.port}/" for a in lan_addresses())
                  + f"  · 허용 IP {len(rules)}개" + ("" if rules else " (없음: 다른 PC 는 모두 거절)"))
        else:
            print("다른 PC 접속: 꺼짐 (이 PC 에서만)")

    listen(srv["host"])
    devs = devices()
    d = device_index(devs, {})
    if d >= 0:
        page(devs[d]["root"], reload=False)   # 기본 장비는 바로 읽기 시작
    if not args.no_open:
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    try:
        while True:
            srv["httpd"].serve_forever()
            srv["httpd"].server_close()
            if not srv["restart"]:
                break
            srv["restart"] = False
            old, new = srv["host"], "0.0.0.0" if access()["lan"] else "127.0.0.1"
            print(f"서버 다시 시작: {old} → {new}")
            try:
                listen(new)
                srv["error"] = ""
            except OSError as e:  # 새 주소로 못 열면 원래 주소로 되돌린다
                srv["error"] = f"{new}:{args.port} 로 열지 못해 {old} 로 되돌렸습니다: {e}"
                print(srv["error"])
                listen(old)
    except KeyboardInterrupt:
        pass
