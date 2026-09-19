"""Web ページの安全な取得と本文の抽出（`08-scrap` 用。すべて同期関数。呼び出し側は asyncio.to_thread 経由で使う）。

安全のための制限（VM の内部アドレスなどへ、URL を経由してアクセスしないため）:
  - http / https のみ、ポートは 80 / 443 のみ
  - 名前解決した IP がすべて公開アドレスであること（プライベート・ループバック・リンクローカル・予約済みを拒否）
  - リダイレクトは自分で追い、**転送先ごとに**同じ検査をする（最大5回）
  - 本文は最大 3MB、画像は最大 5MB まで
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 20.0
ALLOWED_PORTS = {80, 443}
USER_AGENT = "Mozilla/5.0 (compatible; LifeOSBot/1.0; personal use)"
# 全角の句読点・カッコ・空白で URL を区切る（日本語の文章では、URL の直後に「、」「。」が続くため）。
# 半角のカッコは URL の一部になりうる（例: Wikipedia の Foo_(bar)）ので、対応が取れないときだけ外す。
_URL = re.compile(r"https?://[^\s<>\"'\[\]{}（）「」『』【】［］｛｝〈〉《》、。，．！？：；　]+", re.I)
_TWEET = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/]+)/status/(\d+)", re.I)


class FetchError(RuntimeError):
    """取得できなかった。メッセージはそのまま利用者に見せてよい（内部情報を含めない）。"""


class UnsafeUrl(FetchError):
    """安全のために拒否した URL（内部アドレス・http/https 以外・許可外のポートなど）。保存せずに断る。"""


@dataclass
class Article:
    title: str = ""
    text: str = ""
    image_url: str = ""
    site: str = ""
    final_url: str = ""
    kind: str = "web"  # web / x / file（HTML 以外）
    notes: list[str] = field(default_factory=list)


def find_urls(text: str) -> list[str]:
    """メッセージ中の URL（末尾の句読点は除く）。重複は除く。"""
    out: list[str] = []
    for u in _URL.findall(text):
        u = u.rstrip(".,!?;:")
        while u.endswith(")") and u.count("(") < u.count(")"):  # 「(https://…)」の閉じカッコは URL に含めない
            u = u[:-1].rstrip(".,!?;:")
        if u not in out:
            out.append(u)
    return out


def strip_urls(text: str) -> str:
    return _URL.sub(" ", text)


# ---------------------------------------------------------------- 安全性の検査


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_url(url: str, resolver=_resolve) -> None:
    """安全でない URL は FetchError。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("http か https の URL だけ保存できます")
    if not parts.hostname:
        raise UnsafeUrl("URL を読み取れませんでした")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UnsafeUrl("URL のポートが正しくありません") from None
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl("このポートの URL は保存できません")
    try:
        ips = [ipaddress.ip_address(parts.hostname)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(ip.split("%")[0]) for ip in resolver(parts.hostname)]
        except OSError:
            raise UnsafeUrl("このアドレスに接続できませんでした（名前を解決できません）。URL を確認してください") from None
    if not ips or any(not ip.is_global for ip in ips):
        raise UnsafeUrl("内部のアドレスの URL は保存できません")


def _get(client: httpx.Client, url: str, max_bytes: int, resolver=_resolve) -> tuple[httpx.Response, bytes, str]:
    """リダイレクトを1回ずつ検査しながら取得する。(最後の応答, 本文の先頭 max_bytes, 最終 URL)"""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolver)
        try:
            with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
                               follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"ページを取得できませんでした（HTTP {r.status_code}）")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        break
                return r, bytes(body[:max_bytes]), url
        except httpx.TimeoutException:
            raise FetchError("ページの取得がタイムアウトしました") from None
        except httpx.HTTPError as e:
            raise FetchError(f"ページに接続できませんでした（{type(e).__name__}）") from None
    raise FetchError("転送が多すぎるため、取得をやめました")


# ---------------------------------------------------------------- 取得と抽出


def extract_article(html: bytes | str, url: str) -> Article:
    """HTML から、タイトル・本文（Markdown）・アイキャッチ画像の URL を取り出す。"""
    import trafilatura

    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, output_format="markdown") or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    return Article(title=(getattr(meta, "title", "") or "").strip(), text=text.strip(),
                   image_url=(getattr(meta, "image", "") or "").strip(), site=(getattr(meta, "sitename", "") or "").strip(),
                   final_url=url)


def parse_fxtwitter(data: dict, url: str) -> Article:
    """`api.fxtwitter.com` の応答から、X のポストを Article にする。"""
    tw = data.get("tweet") or {}
    if not tw:
        raise FetchError("X のポストを取得できませんでした（非公開・削除済みの可能性があります）")
    author = tw.get("author") or {}
    name, handle = author.get("name", ""), author.get("screen_name", "")
    text = (tw.get("text") or "").strip()
    photos = (tw.get("media") or {}).get("photos") or []
    return Article(title=f"{name}（@{handle}）のポスト" if handle else "X のポスト", text=text, site="X",
                   image_url=(photos[0].get("url", "") if photos else ""), final_url=url, kind="x")


def fetch_article(url: str, client: httpx.Client | None = None, resolver=_resolve) -> Article:
    """URL を取得して Article にする。HTML 以外（PDF・画像など）は、タイトルだけの Article（kind=file）。"""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        m = _TWEET.match(url)
        if m:  # X はログインが必要でページを取得できないため、公開 API 経由で本文を取る
            r, body, _ = _get(client, f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}", MAX_HTML_BYTES, resolver)
            try:
                return parse_fxtwitter(json.loads(body), url)
            except ValueError:
                raise FetchError("X のポストを読み取れませんでした") from None
        r, body, final = _get(client, url, MAX_HTML_BYTES, resolver)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
            name = urlsplit(final).path.rsplit("/", 1)[-1] or urlsplit(final).hostname or "ファイル"
            return Article(title=name, text="", final_url=final, kind="file",
                           notes=[f"HTML ではないファイル（{ctype}）のため、URL だけ保存しました"])
        art = extract_article(body, final)
        if not art.text:
            art.notes.append("本文を取り出せませんでした（ログインが必要なページなどの可能性があります）")
        return art
    finally:
        if own:
            client.close()


def fetch_image(url: str, client: httpx.Client | None = None, resolver=_resolve) -> tuple[bytes, str]:
    """画像を取得する。(バイト列, content-type)。画像でない・大きすぎる場合は FetchError。"""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        r, body, _ = _get(client, url, MAX_IMAGE_BYTES + 1, resolver)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise FetchError("画像ではありません")
        if len(body) > MAX_IMAGE_BYTES:
            raise FetchError("画像が大きすぎます（5MB まで）")
        return body, ctype
    finally:
        if own:
            client.close()
