"""工程テンプレート（SPEC §5 03_CREATIVE「作品管理シート」）。

`06-Life-OS/12-project-others/工程テンプレート.md` に、種類ごとの工程を `## 種類名` ＋ `- 段階` で書く。
Bot は最初に既定の中身で作り、以後はユーザーが Obsidian で自由に直す（Bot は毎回読み直す）。
`## 作品: タイトル` の節があれば、その作品だけはそちらを使う。
"""
from __future__ import annotations

import logging
import re

import notes
import vault_paths

log = logging.getLogger("life-os.stages")

CONTINUING = "続ける"  # 工程を持たない種類（私生活）

DEFAULT = """# 工程テンプレート

種類（作品管理シートの「プロジェクト」列）ごとの工程です。自由に直して大丈夫です。
- `## 種類名` の下に `- 段階` を順に並べます。「小説」は「小説（短編）」「小説（長編）」にも使われます
- 作品ごとに変えたいときは `## 作品: タイトル` の節を作ります（その作品だけ、そちらを使います）
- `## 私生活` のように「- 続ける」とだけ書いた種類は、完成の無い「続ける」型です

## 小説
- 簡易プロット作成
- 詳細プロット作成
- 本文執筆
- 校正01
- 本文改稿01
- 校正02
- 本文改稿02
- 校正03
- 本文改稿03
- キャプション・タグ（あらすじ・タイトルなど）
- 表記の調整（ルビなど）
- 販売・投稿の準備
- 出品・公開

## TRPG
- 文字起こし修正
- 簡易プロット作成
- 詳細プロット作成
- 本文執筆
- HO・資料を整える
- 校正01
- 本文改稿01
- 校正02
- 本文改稿02
- 校正03
- 本文改稿03
- トレーラー・表紙
- キャプション・タグ（説明文・値段など）
- 販売の準備
- 出品・公開

## ストック
- メモ
- プロット
- 執筆
- 校正・校閲01
- 校正・校閲02
- タイトル
- 名前変換
- サムネイル
- Fantia予約投稿
- Xfolio予約投稿
- PDF化

## 開発
- 作るものを決める
- 作る
- 試す
- 公開・販売の準備
- 公開

## 商品・販売
- 素材を選ぶ
- 作る
- 販売ページ
- 出品

## 私生活
- 続ける
"""


def path() -> str:
    return vault_paths.stage_templates()


def parse(text: str) -> dict[str, list[str]]:
    """`## 見出し` → 段階のリスト。見出しはそのまま（`作品: タイトル` も含む）。"""
    out: dict[str, list[str]] = {}
    cur = None
    for raw in (text or "").splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", raw)
        if h:
            cur = h.group(1)
            out.setdefault(cur, [])
            continue
        item = re.match(r"^\s*[-*]\s+(.+?)\s*$", raw)
        if cur and item:
            out[cur].append(item.group(1))
    return out


def steps_for(templates: dict[str, list[str]], kind: str, title: str) -> list[str] | None:
    """その作品の工程。作品の節 → 種類の完全一致 → 種類の前方一致（「小説（短編）」→「小説」）。無ければ None。"""
    import works  # 循環を避ける

    key = works.norm(title)
    for name, steps in templates.items():
        if name.startswith("作品:") and works.norm(name[3:]) == key:
            return steps
    if kind in templates:
        return templates[kind]
    for name, steps in templates.items():
        if not name.startswith("作品:") and kind.startswith(name):
            return steps
    return None


def is_continuing(steps: list[str] | None) -> bool:
    return steps == [CONTINUING]


def load() -> dict[str, list[str]]:
    """ノートを読む。無ければ既定の中身で作る（作れなくても既定を使う）。"""
    store = notes.get_store()
    try:
        text = store.read(path())
    except Exception:  # noqa: BLE001
        log.warning("工程テンプレートを読めませんでした（既定を使います）", exc_info=True)
        return parse(DEFAULT)
    if text is None:
        try:
            store.write(path(), DEFAULT)
        except Exception:  # noqa: BLE001
            log.warning("工程テンプレートを作れませんでした（既定を使います）", exc_info=True)
        return parse(DEFAULT)
    return parse(text)


def next_step(steps: list[str], current: str) -> str | None:
    """今の段階の次。今の段階が空なら最初。テンプレートに無い段階なら None（どれに進めるか聞く）。"""
    if not current:
        return steps[0] if steps else None
    if current in steps:
        i = steps.index(current)
        return steps[i + 1] if i + 1 < len(steps) else None
    return None


def show(steps: list[str], current: str) -> str:
    return "\n".join(f"{'▶' if s == current else '・'} {i}. {s}" for i, s in enumerate(steps, 1))
