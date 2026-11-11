"""뷰어 설정 - 사용자별 %APPDATA%\\audit-pdf-viewer\\config.json

    {"root": "D:\\...\\Audit Trail 리뷰", "recent": [...]}

PDF 루트 폴더는 --root > 설정 파일 순서로 정한다.
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
