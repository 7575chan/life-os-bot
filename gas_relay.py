"""GAS 中継のクライアント（SPEC §2.5）。

サービスアカウントは新規ファイルへ内容を書けないため、新規作成・フォルダ作成・改名・ゴミ箱移動は、
ユーザー権限で動く Google Apps Script のウェブアプリに依頼する。読み取り・更新・一覧の op は存在しない。

- 呼び出す前に必ず notes_policy のチェックを済ませること（GuardedStore が行う）。
- トークンはログや例外メッセージに出さない。
- 通信失敗は同じ request_id で再試行する（GAS 側が再送を検出して二重作成しない）。
"""
import base64
import logging
import uuid

import httpx

import config
import notes_policy as policy
from notes_policy import AccessDenied

log = logging.getLogger("life-os.relay")

MAX_BYTES = 5 * 1024 * 1024
RETRIES = 2  # 初回に加えて最大2回再試行
TIMEOUT = 30.0


class RelayError(RuntimeError):
    """中継の失敗（未設定・通信失敗・GAS 側エラー）。ユーザーには原因の種類だけ伝える。"""


class GasRelay:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        if not url or not token:
            raise RelayError("GAS 中継が未設定です（GAS_RELAY_URL / GAS_RELAY_TOKEN）")
        self._url = url
        self._token = token
        self._client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, transport=transport)

    @classmethod
    def from_config(cls) -> "GasRelay | None":
        if not (config.GAS_RELAY_URL and config.GAS_RELAY_TOKEN):
            return None
        return cls(config.GAS_RELAY_URL, config.GAS_RELAY_TOKEN)

    # -- 内部
    def _post(self, payload: dict) -> dict:
        """トークンと request_id を付けて送信する。応答の JSON（dict）を返す。"""
        body = {**payload, "token": self._token}
        body.setdefault("request_id", uuid.uuid4().hex)
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                r = self._client.post(self._url, content=_dumps(body), headers={"Content-Type": "text/plain;charset=utf-8"})
                if r.status_code >= 500:
                    raise RelayError(f"中継が一時的に応答できません (HTTP {r.status_code})")
                if r.status_code != 200:
                    raise RelayError(f"中継が想定外の応答を返しました (HTTP {r.status_code})")
                try:
                    return r.json()
                except ValueError:
                    raise RelayError("中継の応答を解釈できません（デプロイ設定を確認してください）") from None
            except (httpx.HTTPError, RelayError) as e:
                last = e
                log.warning("relay op=%s attempt=%d failed: %s", payload.get("op"), attempt + 1, type(e).__name__)
                if isinstance(e, RelayError) and "想定外" in str(e):
                    break  # 4xx などは再試行しても変わらない
        if isinstance(last, RelayError):
            raise last
        raise RelayError(f"中継に接続できません ({type(last).__name__})")

    def _call(self, op: str, path: str, **fields) -> dict:
        res = self._post({"op": op, "path": path, **fields})
        status = res.get("status")
        if status == "denied":
            policy.log.error("DENY (GAS relay) op=%s path=%r reason=%s", op, path, res.get("message"))
            raise AccessDenied(f"中継が許可領域外として拒否しました: {op} {path!r}")
        if status == "unauthorized":
            raise RelayError("中継の認証に失敗しました（GAS_RELAY_TOKEN が一致していません）")
        if status == "not_found":
            raise FileNotFoundError(path)
        if not res.get("ok") and status != "exists":
            raise RelayError(f"中継でエラーが発生しました: {str(res.get('message', ''))[:120]}")
        return res

    # -- 公開 API（op はこの4つだけ）
    def create_file(self, path: str, text: str) -> dict:
        """新規作成。既存なら {"status": "exists", "id": ...}（上書きしない）。"""
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise RelayError("ファイルが大きすぎます（5MB まで）")
        return self._call("create_file", path, content=text)

    def create_bytes(self, path: str, data: bytes, mime: str = "application/octet-stream") -> dict:
        if len(data) > MAX_BYTES:
            raise RelayError("ファイルが大きすぎます（5MB まで）")
        return self._call("create_bytes", path, content_base64=base64.b64encode(data).decode("ascii"), mime=mime)

    def rename(self, old: str, new: str) -> dict:
        return self._call("rename", old, new_path=new)

    def trash(self, path: str) -> dict:
        return self._call("trash", path)


def _dumps(obj: dict) -> bytes:
    import json

    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
