"""뷰어 설정 - 사용자별 %APPDATA%\\audit-pdf-viewer\\config.json

    {"root": "D:\\...\\COU-P001", "recent": [...],
     "devices": [{"name": "[COU-P001]12열계수기", "root": "D:\\...\\COU-P001"}, ...],
     "lan": true, "allow": [{"ip": "192.168.10.21", "name": "품질팀 PC"}, ...],
     "wc_devices": [{"name": "[WCM-P001]중량선별기", "root": "C:\\#Inbox\\WC", "stage": "test"}]}

PDF 루트 폴더는 --root > 설정 파일 순서로 정한다. 뷰어의 장비 탭은 devices 의 폴더 중 하나를 root 로 고른다.
"""

import json
import os
from pathlib import Path

RECENT_MAX = 8
CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "audit-pdf-viewer" / "config.json"


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_root(root):
    """PDF 루트 폴더를 저장하고 최근 목록 맨 앞에 올린다."""
    cfg = load_config()
    root = str(root)
    cfg["root"] = root
    recent = [p for p in cfg.get("recent", []) if isinstance(p, str) and p.lower() != root.lower()]
    cfg["recent"] = [root] + recent[:RECENT_MAX - 1]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def devices(cfg=None):
    """장비 목록 [{"name", "root"}]. 장비 목록이 없던 예전 설정이면 지금 폴더 하나를 장비로 본다."""
    cfg = load_config() if cfg is None else cfg
    out = [{"name": str(d.get("name") or "").strip(), "root": str(d.get("root") or "").strip()}
           for d in cfg.get("devices", []) if isinstance(d, dict)]
    out = [d for d in out if d["root"]]
    if not out and cfg.get("root"):
        out = [{"name": Path(cfg["root"]).name, "root": cfg["root"]}]
    for d in out:
        d["name"] = d["name"] or Path(d["root"]).name
    return out


def save_devices(devs, root):
    """장비 목록을 저장하고 root 를 지금 폴더로 정한다 (report/export/diagnose 도 이 폴더를 쓴다)."""
    cfg = load_config()
    cfg["devices"] = devs
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return save_root(root)


def wc_devices(cfg=None):
    """중량선별기 장비 [{"name", "root", "stage"}]. stage 가 "test" 면 테스트 페이지(/lab)에서만 보인다."""
    cfg = load_config() if cfg is None else cfg
    out = []
    for d in cfg.get("wc_devices", []):
        if isinstance(d, dict) and str(d.get("root") or "").strip():
            root = str(d["root"]).strip()
            out.append({"name": str(d.get("name") or "").strip() or Path(root).name, "root": root,
                        "stage": "live" if d.get("stage") == "live" else "test"})
    return out


def save_wc_devices(devs):
    cfg = load_config()
    cfg["wc_devices"] = devs
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def access(cfg=None):
    """다른 PC 접속 설정 {"lan": bool, "allow": [{"ip", "name"}]}.

    lan: 서버를 내부망에 연다 (서버를 다시 시작하면 적용). allow: 접속을 허용할 IP 또는 대역(예: 192.168.10.0/24).
    """
    cfg = load_config() if cfg is None else cfg
    allow = [{"ip": str(a.get("ip") or "").strip(), "name": str(a.get("name") or "").strip()}
             for a in cfg.get("allow", []) if isinstance(a, dict)]
    return {"lan": bool(cfg.get("lan")), "allow": [a for a in allow if a["ip"]]}


def save_access(lan, allow):
    cfg = load_config()
    cfg["lan"], cfg["allow"] = bool(lan), allow
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def configured_root():
    return load_config().get("root") or ""


def check_root(root):
    """PDF 루트 폴더인지 확인한다. 문제가 있으면 이유 문자열, 없으면 None."""
    if not root:
        return "PDF 루트 폴더를 지정하세요."
    root = Path(root)
    if not root.is_dir():
        return f"폴더가 없습니다: {root}"
    if root.parent == root:
        return f"드라이브 전체는 지정할 수 없습니다. 리포트가 있는 폴더를 고르세요: {root}"
    return None


def browse_folder(initial=None):
    """이 PC 에 폴더 선택 창을 띄운다. 취소하면 빈 문자열."""
    import tkinter
    from tkinter import filedialog

    tk = tkinter.Tk()
    tk.withdraw()
    tk.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(parent=tk, title="Audit Trail PDF 루트 폴더 선택",
                                       initialdir=initial if initial and Path(initial).is_dir() else None,
                                       mustexist=True)
    finally:
        tk.destroy()
    return str(Path(path)) if path else ""
