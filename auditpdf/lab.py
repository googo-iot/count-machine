"""중량선별기 화면 - 테스트(/lab)와 운영(/wc)이 같은 처리와 같은 화면 파일(wc_template.html)을 쓴다.

장비 목록은 wc_devices 하나이고, 장비마다 stage 가 있다.
    test  테스트 페이지(/lab, 서버 PC 에서만)에만 보인다
    live  운영 화면(/wc, 허용 IP 의 다른 PC 도 보기 전용)에도 보인다
장비 추가·변경·삭제와 운영 표시는 테스트 페이지의 '장비 설정'에서만 한다 (서버 PC 관리자).

    GET  {base}?d=번호                    화면              (base = /lab 또는 /wc, d 는 그 화면의 장비 목록 번호)
    GET  {base}/api/data?d=번호[&reload]  장비 데이터 (gzip JSON). reload 는 관리자만
    GET  {base}/run?d=번호&f=파일번호       검사 런 하나의 중량 그래프 (새 창으로 여는 독립 화면)
    GET  {base}/audit?d=번호&f=파일번호     그 런의 작업 일자 Audit Trail 창
    GET  {base}/api/run?d=번호&f=파일번호[&norows]  검사 런 하나의 요약과 행, 그날의 이력(Audit Trail)
    POST /lab/api/device                 장비 추가 / 변경 / 삭제 / 운영 표시 (관리자)
"""

import gzip
import json
import threading
from datetime import date, timedelta
from pathlib import Path

from . import wc
from .config import check_root, save_wc_devices, wc_devices

TEMPLATE = Path(__file__).with_name("wc_template.html")
SET_PLACEHOLDER = "/*__SET__*/null"


class Lab:
    def __init__(self):
        self.lock = threading.Lock()   # 같은 장비를 동시에 두 번 읽지 않게
        self.data = {}                 # 폴더(소문자) → (dict, gzip JSON 관리자용, gzip JSON 보기 전용)

    @staticmethod
    def device(q, live):
        """(이 화면의 장비 목록, 고른 번호). 운영 화면은 stage 가 live 인 장비만."""
        devs = [{**x, "i": i} for i, x in enumerate(wc_devices()) if not live or x["stage"] == "live"]
        try:
            d = int(q.get("d", ["0"])[0])
        except ValueError:
            d = 0
        return devs, (d if 0 <= d < len(devs) else (0 if devs else -1))

    def load(self, root, reload=False):
        key = root.lower()
        with self.lock:
            if reload or key not in self.data:
                error = check_root(root)
                if error:
                    raise ValueError(error)
                data = wc.scan(root)
                pack = lambda x: gzip.compress(json.dumps(x, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
                # 보기 전용(다른 PC)에는 서버의 폴더 경로를 보내지 않는다
                self.data[key] = (data, pack(data), pack({**data, "root": ""}))
            return self.data[key]

    def get(self, h, path, q, live=False, admin=True):
        base = "/wc" if live else "/lab"
        sub = path[len(base):]
        devs, d = self.device(q, live)
        if sub in ("", "/run", "/audit"):
            s = {"devices": [{"name": x["name"], "stage": x["stage"], **({"root": x["root"], "i": x["i"]} if admin else {})} for x in devs],
                 "device": d, "admin": admin, "live": live, "base": base}
            if sub == "/run":     # 같은 화면 파일을 그래프 하나만 보이는 모양으로 연다
                s["run"] = int(q.get("f", ["-1"])[0])
            if sub == "/audit":   # 그래프 창의 Audit Trail 을 따로 띄운 창
                s["audit"] = int(q.get("f", ["-1"])[0])
            html = TEMPLATE.read_text(encoding="utf-8").replace(
                SET_PLACEHOLDER, json.dumps(s, ensure_ascii=False).replace("</", "<\\/"), 1)
            return h.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if d < 0:
            return h.send_json(404, {"error": "운영 화면에 올린 중량선별기 장비가 없습니다." if live
                                     else "중량선별기 장비가 없습니다. '+ 장비 추가'로 등록하세요."})
        root = devs[d]["root"]
        if sub == "/api/data":
            try:
                _, full, public = self.load(root, reload="reload" in q and admin)
            except ValueError as e:
                return h.send_json(400, {"error": str(e) if admin else "장비 데이터를 읽지 못했습니다. 관리자에게 알려 주세요."})
            return h.send(200, full if admin else public, "application/json; charset=utf-8", [("Content-Encoding", "gzip")])
        if sub == "/api/run":
            data = self.load(root)[0]
            try:
                fi = int(q["f"][0])
            except (KeyError, ValueError):
                return h.send_json(400, {"error": "런 번호가 없습니다."})
            run = next((r for r in data["runs"] if r[0] == fi), None)
            if run is None:
                return h.send_json(404, {"error": "검사 기록 런이 아닙니다. 목록 화면에서 다시 여세요."})
            rel = data["files"][fi][0]
            # Audit Trail: 런 날짜(자정을 넘긴 런이면 다음 날까지)의 이력. 작업 전·중·후 구분은 화면에서
            days = {run[1]}
            if run[3] < run[2]:
                days.add((date.fromisoformat(run[1]) + timedelta(days=1)).isoformat())
            hist = [x[:9] + [data["files"][x[9]][0].split("\\")[-1]] for x in data["history"] if x[0] in days]
            body = {"device": devs[d]["name"], "file": rel, "run": run, "history": hist,
                    "rows": [] if "norows" in q else wc.run_detail(root, rel)}
            return h.send(200, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                          "application/json; charset=utf-8")
        h.send(404, b"not found", "text/plain")

    def post(self, h, path, body):
        if path != "/lab/api/device":
            return h.send_json(404, {"error": "not found"})
        devs = wc_devices()
        index = body.get("index")
        if index is not None:
            try:
                index = int(index)
                devs[index]
            except (TypeError, ValueError, IndexError):
                return h.send_json(400, {"error": "없는 장비입니다."})
        if body.get("delete"):
            if index is None:
                return h.send_json(400, {"error": "지울 장비를 고르세요."})
            gone = devs.pop(index)
            with self.lock:
                self.data.pop(gone["root"].lower(), None)
            save_wc_devices(devs)
            return h.send_json(200, {"index": 0 if devs else -1})
        name = str(body.get("name", "")).strip()
        root = str(body.get("root", "")).strip().strip('"')
        if not name:
            return h.send_json(400, {"error": "장비 이름을 입력하세요."})
        if not root:
            return h.send_json(400, {"error": "폴더 경로를 입력하세요."})
        root = str(Path(root).expanduser())
        error = check_root(root)
        if error:
            return h.send_json(400, {"error": error})
        if any(i != index and x["name"] == name for i, x in enumerate(devs)):
            return h.send_json(400, {"error": f"같은 이름의 장비가 있습니다: {name}"})
        stage = "live" if body.get("live") else "test"
        if index is None:
            devs.append({"name": name, "root": root, "stage": stage})
            index = len(devs) - 1
        else:
            if devs[index]["root"].lower() != root.lower():
                with self.lock:
                    self.data.pop(devs[index]["root"].lower(), None)
            devs[index] = {**devs[index], "name": name, "root": root, "stage": stage}
        save_wc_devices(devs)
        print(f"중량선별기 장비 저장: {name} → {root} ({'운영' if stage == 'live' else '테스트'})")
        h.send_json(200, {"index": index})
