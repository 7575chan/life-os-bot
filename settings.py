"""実行時設定 (data/settings.json)。14-ai の「システムのカスタマイズ」で書き換える。"""
import json

import config

_PATH = config.DATA_DIR / "settings.json"

DEFAULTS = {
    "summary_length": "3行",  # 08-scrap の要約の長さ
    "idea_tags": ["小説", "TRPG", "仕事", "日常", "開発"],
    "scrap_tag_count": "3〜5個",
}

ALLOWED_KEYS = set(DEFAULTS)


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return data


def get(key: str):
    return load()[key]


def set_value(key: str, value) -> None:
    if key not in ALLOWED_KEYS:
        raise KeyError(f"変更できる設定は {sorted(ALLOWED_KEYS)} のみです")
    data = load()
    data[key] = value
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
