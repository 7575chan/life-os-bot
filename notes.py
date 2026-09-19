"""Obsidian (.md) 保存。local / Google Drive の2バックエンド + アクセスポリシーによるガード。

- パスは常に Vault ルートからの相対 POSIX パス (例: "06-Life-OS/09-idea/Ideas.md")。
- 公開されるのは `get_store()` が返す GuardedStore のみ。全操作が notes_policy を通る。
- 読み取りは Vault 全体で可。書き込み・改名・削除は `06-Life-OS/` とタスク棚のみ。
- Drive 認証はサービスアカウント (credentials.json) のみ。OAuth は使わない。
- すべて同期関数。呼び出し側は asyncio.to_thread 経由で使う。
"""
from __future__ import annotations

import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import config
import gas_relay
import notes_policy as policy
from notes_policy import LIFEOS_DIR, TASK_FILE, VAULT_ROOT, AccessDenied  # noqa: F401

# ---------------------------------------------------------------- Markdown 編集 (純粋関数)


def insert_below_header(text: str, entry: str, title: str) -> str:
    """先頭の '# 見出し' 直下(最上部)に entry を挿入する。無ければ見出しごと新規作成。"""
    entry = entry.strip("\n")
    if not text.strip():
        return f"# {title}\n\n{entry}\n"
    lines = text.split("\n")
    idx = 0
    if lines[0].strip() == "---":  # フロントマターを飛ばす
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                idx = i + 1
                break
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].startswith("# "):
        head, rest = lines[: idx + 1], lines[idx + 1 :]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return "\n".join(head) + "\n\n" + entry + "\n\n" + "\n".join(rest).rstrip("\n") + "\n"
    return "\n".join(lines[:idx]) + ("\n" if idx else "") + entry + "\n\n" + "\n".join(lines[idx:]).rstrip("\n") + "\n"


def append_entry(text: str, entry: str, title: str) -> str:
    entry = entry.strip("\n")
    if not text.strip():
        return f"# {title}\n\n{entry}\n"
    return text.rstrip("\n") + "\n\n" + entry + "\n"


def replace_title(text: str, new_title: str) -> str:
    """ノート内の最初の '# ' 見出しを差し替える。無ければ先頭に付ける。"""
    if re.search(r"^# .*$", text, re.M):
        return re.sub(r"^# .*$", f"# {new_title}", text, count=1, flags=re.M)
    return f"# {new_title}\n\n{text}"


def set_meta_line(text: str, key: str, value: str) -> str:
    """'key: value' 形式のメタデータ行を差し替える (無ければフロントマター先頭に追加)。"""
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if pattern.search(text):
        return pattern.sub(f"{key}: {value}", text, count=1)
    if text.startswith("---\n"):
        return text.replace("---\n", f"---\n{key}: {value}\n", 1)
    return f"---\n{key}: {value}\n---\n\n{text}"


def snippet(text: str, words: list[str], radius: int = 60) -> str:
    low = text.lower()
    pos = min((low.find(w) for w in words if low.find(w) != -1), default=0)
    s = max(0, pos - radius)
    return text[s : pos + radius].replace("\n", " ")


def _matches(text: str, words: list[str]) -> bool:
    low = text.lower()
    return all(w in low for w in words)


# ---------------------------------------------------------------- バックエンド（直接使用禁止。GuardedStore 経由）


class LocalStore:
    """Vault ディレクトリを直接操作する (開発用)。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Vault が見つかりません: {self.root}")

    def _p(self, path: str, op: str) -> Path:
        p = (self.root / path).resolve() if path else self.root
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            raise AccessDenied("Vault の外にはアクセスできません") from None
        # シンボリックリンク等で許可領域の外を指していないか、解決後のパスで再検証する
        if not policy.is_allowed(VAULT_ROOT if rel == "." else rel, op):
            raise AccessDenied(f"解決後のパスがポリシー外です: {op} {rel!r}")
        return p

    def read(self, path: str) -> str | None:
        p = self._p(path, "read")
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write(self, path: str, text: str) -> str:
        p = self._p(path, "write")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    def write_bytes(self, path: str, data: bytes, mime: str = "application/octet-stream") -> str:
        p = self._p(path, "write")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def rename(self, old: str, new: str) -> None:
        src, dst = self._p(old, "rename"), self._p(new, "rename")
        if dst.exists():
            raise FileExistsError(new)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def delete(self, path: str) -> None:
        """完全削除はせず `06-Life-OS/.trash/` へ退避する。"""
        src = self._p(path, "delete")
        if not src.is_file():
            raise FileNotFoundError(path)
        trash = self._p(f"{LIFEOS_DIR}/.trash", "write")
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(trash / f"{datetime.now():%Y%m%d%H%M%S}_{src.name}"))

    def list(self, prefix: str) -> list[str]:
        base = self._p(prefix, "list")
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix() for p in base.rglob("*.md") if ".trash" not in p.parts
        )

    def modified(self, path: str) -> str | None:
        p = self._p(path, "read")
        return str(p.stat().st_mtime_ns) if p.is_file() else None

    # -- メタデータ（多重起動の「使用中の印」用。Vault の中には何も書かず data/ に保存する）
    def _props_file(self) -> Path:
        return config.DATA_DIR / "local_props.json"

    def _all_props(self) -> dict:
        import json

        try:
            return json.loads(self._props_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get_props(self, path: str) -> dict:
        self._p(path, "read")
        return dict(self._all_props().get(path, {}))

    def set_props(self, path: str, props: dict) -> None:
        import json

        self._p(path, "write")
        data = self._all_props()
        cur = data.setdefault(path, {})
        for k, v in props.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = str(v)
        self._props_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class DriveStore:
    """Google Drive API v3（サービスアカウント認証のみ）+ GAS 中継。

    サービスアカウントは容量が無いため、**何も作成しない**（読み取りと、既存ファイルの内容更新だけ）。
    新規ファイル・画像・フォルダの作成、改名、ゴミ箱移動は GAS 中継（gas_relay）に依頼する（SPEC §2.5）。

    起点となるフォルダ/ファイルの ID:
      - lifeos_id   `06-Life-OS` フォルダ（編集者）: 書き込みはここの配下だけ
      - taskfile_id `01 Life-OS-Task.md`（編集者）: ID で直接読み書きする
      - vault_id    Vault ルート（閲覧者）: 許可外ノートの読み取り・検索用。未設定なら読めない
    """

    FOLDER = "application/vnd.google-apps.folder"
    SCOPES = ["https://www.googleapis.com/auth/drive"]
    _FIELDS = "id,name,mimeType,parents,webViewLink,modifiedTime"

    def __init__(self, lifeos_id: str, taskfile_id: str, vault_id: str, credentials_file: str = "",
                 relay=None, svc=None):
        if not lifeos_id:
            raise RuntimeError("OBSIDIAN_LIFEOS_FOLDER_ID が未設定です")
        if svc is None:  # テストでは偽の svc を渡す
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=self.SCOPES)
            svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.svc = svc
        self.relay = relay
        self.lifeos_id, self.taskfile_id, self.vault_id = lifeos_id, taskfile_id, vault_id
        self._folder_map: tuple[float, dict[str, str]] | None = None

    @classmethod
    def from_config(cls):
        return cls(config.OBSIDIAN_LIFEOS_FOLDER_ID, config.OBSIDIAN_TASKFILE_ID,
                   config.OBSIDIAN_VAULT_FOLDER_ID, config.CREDENTIALS_FILE, relay=gas_relay.GasRelay.from_config())

    def _need_relay(self):
        if self.relay is None:
            raise gas_relay.RelayError("GAS 中継が未設定です（GAS_RELAY_URL / GAS_RELAY_TOKEN）")
        return self.relay

    # -- helpers
    @staticmethod
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    def _list(self, q: str, fields: str | None = None) -> list[dict]:
        out, token = [], None
        while True:
            r = (
                self.svc.files()
                .list(q=q, fields=f"nextPageToken,files({fields or self._FIELDS})", pageSize=200,
                      pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True)
                .execute()
            )
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def _child(self, parent: str, name: str) -> dict | None:
        res = self._list(f"'{parent}' in parents and name='{self._esc(name)}' and trashed=false")
        return res[0] if res else None

    def _root(self, n: str) -> tuple[str, list[str], bool]:
        """(起点フォルダID, 起点からの残りパス, 書き込み領域か)。"""
        if n == VAULT_ROOT:
            if not self.vault_id:
                raise FileNotFoundError("OBSIDIAN_VAULT_FOLDER_ID が未設定のため Vault 全体は読めません")
            return self.vault_id, [], False
        if n == LIFEOS_DIR or n.startswith(LIFEOS_DIR + "/"):
            return self.lifeos_id, n.split("/")[1:], True
        if not self.vault_id:
            raise FileNotFoundError("OBSIDIAN_VAULT_FOLDER_ID が未設定のため許可外ノートは読めません")
        return self.vault_id, n.split("/"), False

    def _dir(self, root: str, parts: list[str]) -> str | None:
        """root から parts を辿ったフォルダの ID。無ければ None（サービスアカウントは作成しない）。"""
        cur = root
        for part in parts:
            found = self._child(cur, part)
            if found is None:
                return None
            cur = found["id"]
        return cur

    def _locate(self, path: str):
        """(ファイル情報|None, 親フォルダID|None, ファイル名)。"""
        n = VAULT_ROOT if path in (VAULT_ROOT, ".") else policy.normalize(path)
        if n == TASK_FILE and self.taskfile_id:
            meta = self.svc.files().get(fileId=self.taskfile_id, fields=self._FIELDS,
                                        supportsAllDrives=True).execute()
            return meta, None, meta["name"]
        root, parts, _ = self._root(n)
        if not parts:
            return {"id": root, "name": n or "(vault)", "mimeType": self.FOLDER}, None, n
        parent = self._dir(root, parts[:-1])
        if parent is None:
            return None, None, parts[-1]
        return self._child(parent, parts[-1]), parent, parts[-1]

    # -- API
    def read(self, path: str) -> str | None:
        f, _, _ = self._locate(path)
        if not f:
            return None
        data = self.svc.files().get_media(fileId=f["id"], supportsAllDrives=True).execute()
        return data.decode("utf-8")

    def _update_existing(self, file_id: str, data: bytes, mime: str) -> str:
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        r = self.svc.files().update(fileId=file_id, media_body=media, fields="id,webViewLink",
                                    supportsAllDrives=True).execute()
        return r.get("webViewLink", r["id"])

    def _put(self, path: str, data: bytes, mime: str, text: str | None) -> str:
        """既存ならサービスアカウントで更新、無ければ GAS 中継で新規作成（親フォルダも中継が作る）。"""
        n = policy.normalize(path)
        if not policy.is_allowed(n, "write"):  # バックエンド側でも二重に確認
            raise AccessDenied(f"書き込み許可リスト外です: {n!r}")
        f, _, _ = self._locate(n)
        if f:
            return self._update_existing(f["id"], data, mime)
        relay = self._need_relay()
        res = relay.create_file(n, text) if text is not None else relay.create_bytes(n, data, mime)
        if res.get("status") == "exists":  # 反映遅延などで見えていなかった既存ファイル
            f, _, _ = self._locate(n)
            if f:
                return self._update_existing(f["id"], data, mime)
        return res.get("url") or res.get("id", "")

    def write(self, path: str, text: str) -> str:
        return self._put(path, text.encode("utf-8"), "text/markdown", text)

    def write_bytes(self, path: str, data: bytes, mime: str = "application/octet-stream") -> str:
        return self._put(path, data, mime, None)

    def rename(self, old: str, new: str) -> None:
        for p in (old, new):
            if not policy.is_allowed(policy.normalize(p), "rename"):
                raise AccessDenied(f"書き込み許可リスト外です: {p!r}")
        self._need_relay().rename(policy.normalize(old), policy.normalize(new))

    def delete(self, path: str) -> None:
        """完全削除ではなくゴミ箱へ移動する（復元可能）。中継が実行する。"""
        n = policy.normalize(path)
        if not policy.is_allowed(n, "delete"):
            raise AccessDenied(f"書き込み許可リスト外です: {n!r}")
        self._need_relay().trash(n)

    def list(self, prefix: str) -> list[str]:
        n = VAULT_ROOT if prefix in (VAULT_ROOT, ".") else policy.normalize(prefix)
        root, parts, _ = self._root(n)
        start = self._dir(root, parts) if parts else root
        if start is None:
            return []
        out, stack = [], [(start, n)]
        while stack:
            fid, base = stack.pop()
            for item in self._list(f"'{fid}' in parents and trashed=false"):
                rel = f"{base}/{item['name']}" if base else item["name"]
                if item["mimeType"] == self.FOLDER:
                    stack.append((item["id"], rel))
                elif item["name"].endswith(".md"):
                    out.append(rel)
        return sorted(out)

    def modified(self, path: str) -> str | None:
        f, _, _ = self._locate(path)
        return f["modifiedTime"] if f else None

    # -- メタデータ（appProperties）: ファイルの中身を変えず、Obsidian にも見えない。多重起動の「使用中の印」用
    def get_props(self, path: str) -> dict:
        f, _, _ = self._locate(path)
        if not f:
            raise FileNotFoundError(path)
        r = self.svc.files().get(fileId=f["id"], fields="appProperties", supportsAllDrives=True).execute()
        return dict(r.get("appProperties") or {})

    def set_props(self, path: str, props: dict) -> None:
        """props の値が None のキーは削除する。書き込み許可領域の中のフォルダ/ファイルだけ。"""
        n = policy.normalize(path)
        if not policy.is_allowed(n, "write"):
            raise AccessDenied(f"書き込み許可リスト外です: {n!r}")
        f, _, _ = self._locate(n)
        if not f:
            raise FileNotFoundError(path)
        self.svc.files().update(fileId=f["id"], body={"appProperties": props}, fields="id",
                                supportsAllDrives=True).execute()

    def search(self, words: list[str], limit: int, scope: str) -> list[dict]:
        """Drive の全文検索 API を使い、全ノートの本文を毎回ダウンロードしない。"""
        root_id = self.lifeos_id if scope == LIFEOS_DIR else self.vault_id
        if not root_id:
            raise FileNotFoundError("OBSIDIAN_VAULT_FOLDER_ID が未設定のため Vault 全体は検索できません")
        folders = self._folder_paths(root_id, scope)
        q = " and ".join(f"fullText contains '{self._esc(w)}'" for w in words) + " and trashed=false"
        hits = []
        for item in self._list(q):
            if not item["name"].endswith(".md") or not item.get("parents"):
                continue
            base = folders.get(item["parents"][0])
            if base is None:
                continue  # 検索範囲外のフォルダ
            path = f"{base}/{item['name']}" if base else item["name"]
            data = self.svc.files().get_media(fileId=item["id"], supportsAllDrives=True).execute()
            text = data.decode("utf-8", errors="ignore")
            hits.append({"path": path, "snippet": snippet(text, words)})
            if len(hits) >= limit:
                break
        return hits

    def _folder_paths(self, root_id: str, root_path: str) -> dict[str, str]:
        """フォルダID -> Vault相対パス（5分キャッシュ）。"""
        key = root_id
        if self._folder_map and self._folder_map[0] > time.time() and key in self._folder_map[1]:
            return self._folder_map[1][key]
        mapping = {root_id: root_path}
        stack = [root_id]
        while stack:
            fid = stack.pop()
            for item in self._list(f"'{fid}' in parents and mimeType='{self.FOLDER}' and trashed=false"):
                mapping[item["id"]] = f"{mapping[fid]}/{item['name']}" if mapping[fid] else item["name"]
                stack.append(item["id"])
        cache = self._folder_map[1] if self._folder_map else {}
        cache[key] = mapping
        self._folder_map = (time.time() + 300, cache)
        return mapping


# ---------------------------------------------------------------- 公開API: GuardedStore


class GuardedStore:
    """全操作の前に notes_policy.check() を通す。ポリシー違反は AccessDenied（ログ記録）で中断する。"""

    def __init__(self, backend):
        self._b = backend

    def read(self, path: str) -> str | None:
        return self._b.read(policy.check(path, "read"))

    def write(self, path: str, text: str) -> str:
        return self._b.write(policy.check(path, "write"), text)

    def write_bytes(self, path: str, data: bytes, mime: str = "application/octet-stream") -> str:
        return self._b.write_bytes(policy.check(path, "write"), data, mime)

    def rename(self, old: str, new: str) -> None:
        # 移動元・移動先の両方が書き込み許可内であること
        self._b.rename(policy.check(old, "rename"), policy.check(new, "rename"))

    def delete(self, path: str) -> None:
        self._b.delete(policy.check(path, "delete"))

    def list(self, prefix: str = LIFEOS_DIR) -> list[str]:
        return self._b.list(policy.check(prefix, "list"))

    def modified(self, path: str) -> str | None:
        return self._b.modified(policy.check(path, "read"))

    def get_props(self, path: str) -> dict:
        return self._b.get_props(policy.check(path, "read"))

    def set_props(self, path: str, props: dict) -> None:
        self._b.set_props(policy.check(path, "write"), props)

    def search(self, query: str, limit: int = 10, scope: str = LIFEOS_DIR) -> list[dict]:
        """scope: `06-Life-OS`（既定）または "" (Vault 全体)。許可外ノートの検索は依頼時のみ使うこと。"""
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []
        policy.check(scope, "list")
        if hasattr(self._b, "search"):
            return self._b.search(words, limit, scope)
        candidates = self.list(scope)
        if scope == LIFEOS_DIR and TASK_FILE not in candidates:
            candidates.append(TASK_FILE)
        hits = []
        for path in candidates:
            try:
                text = self.read(path) or ""
            except FileNotFoundError:
                continue
            if _matches(path + "\n" + text, words):
                hits.append({"path": path, "snippet": snippet(text, words)})
                if len(hits) >= limit:
                    break
        return hits

    # -- 編集ヘルパー
    def prepend_entry(self, path: str, entry: str, title: str) -> str:
        return self.write(path, insert_below_header(self.read(path) or "", entry, title))

    def append_entry(self, path: str, entry: str, title: str) -> str:
        return self.write(path, append_entry(self.read(path) or "", entry, title))


_store: GuardedStore | None = None


def get_store() -> GuardedStore:
    global _store
    if _store is None:
        backend = LocalStore(config.OBSIDIAN_VAULT_DIR) if config.OBSIDIAN_BACKEND == "local" else DriveStore.from_config()
        _store = GuardedStore(backend)
    return _store
