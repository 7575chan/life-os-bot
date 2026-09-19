import json

import httpx
import pytest

import webfetch as wf
from webfetch import FetchError

HOSTS = {
    "example.com": ["93.184.216.34"],
    "news.example.org": ["203.0.113.10", "203.0.113.11"],  # ドキュメント用アドレスは is_global=False なので下で別途
    "internal.test": ["10.0.0.5"],
    "meta.test": ["169.254.169.254"],
    "local.test": ["127.0.0.1"],
    "v6.test": ["::1"],
    "mixed.test": ["93.184.216.34", "192.168.1.10"],
    "cdn.example.net": ["8.8.4.4"],
}
PUBLIC = {"example.com": ["93.184.216.34"], "b.example.net": ["8.8.8.8"], "cdn.example.net": ["8.8.4.4"],
          "api.fxtwitter.com": ["8.8.4.4"]}


def resolver(host):
    table = {**HOSTS, **PUBLIC}
    if host not in table:
        raise OSError("no such host")
    return table[host]


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- 安全性の検査


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)", "gopher://example.com",
    "http://example.com:8080/", "https://example.com:22/", "http://10.0.0.1/", "http://127.0.0.1/", "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/", "http://192.168.0.1/", "http://0.0.0.0/", "http://100.64.0.1/",
    "http://internal.test/", "http://meta.test/", "http://local.test/", "http://v6.test/", "http://mixed.test/",
    "http:///path", "https://",
])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(FetchError):
        wf.check_url(url, resolver)


def test_public_urls_are_allowed():
    for url in ("https://example.com/a?b=1#c", "http://example.com/", "https://8.8.8.8/x", "https://example.com:443/"):
        wf.check_url(url, resolver)


def test_unsafe_url_errors_are_a_distinct_type_so_they_are_not_saved_as_bookmarks():
    with pytest.raises(wf.UnsafeUrl):
        wf.check_url("http://10.0.0.1/", resolver)
    assert issubclass(wf.UnsafeUrl, FetchError)

    def not_found(request):
        return httpx.Response(404)

    with client_for(not_found) as c, pytest.raises(FetchError) as ei:  # 404 は「安全でない」ではない（ブックマークとして保存できる）
        wf.fetch_article("https://example.com/x", c, resolver)
    assert not isinstance(ei.value, wf.UnsafeUrl)


def test_unresolvable_host_is_a_clear_error():
    with pytest.raises(FetchError, match="名前を解決"):
        wf.check_url("https://no-such-host.example/", resolver)


# ---------------------------------------------------------------- 取得（リダイレクトは転送先ごとに検査）


def html_response(body="<html><head><title>T</title></head><body><p>x</p></body></html>", ctype="text/html; charset=utf-8"):
    return httpx.Response(200, content=body.encode(), headers={"content-type": ctype})


def test_redirect_to_an_internal_address_is_blocked():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://meta.test/latest/meta-data/"})
        return html_response("SECRET")

    with client_for(handler) as c, pytest.raises(FetchError, match="内部"):
        wf.fetch_article("https://example.com/x", c, resolver)
    assert seen == ["https://example.com/x"]  # 転送先には、接続すらしない


def test_redirect_with_relative_location_and_final_url():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html_response("<html><head><title>新</title></head><body><article><p>" + "本文です。" * 40 + "</p></article></body></html>")

    with client_for(handler) as c:
        art = wf.fetch_article("https://example.com/old", c, resolver)
    assert art.final_url == "https://example.com/new" and art.title == "新"


def test_too_many_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    with client_for(handler) as c, pytest.raises(FetchError, match="転送が多すぎ"):
        wf.fetch_article("https://example.com/loop", c, resolver)


def test_http_error_and_timeout_messages():
    with client_for(lambda r: httpx.Response(404)) as c, pytest.raises(FetchError, match="HTTP 404"):
        wf.fetch_article("https://example.com/x", c, resolver)

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with client_for(slow) as c, pytest.raises(FetchError, match="タイムアウト"):
        wf.fetch_article("https://example.com/x", c, resolver)

    def down(request):
        raise httpx.ConnectError("refused")

    with client_for(down) as c, pytest.raises(FetchError, match="接続できません"):
        wf.fetch_article("https://example.com/x", c, resolver)


def test_body_is_capped_at_3mb():
    big = b"a" * (wf.MAX_HTML_BYTES * 2)
    with client_for(lambda r: httpx.Response(200, content=big, headers={"content-type": "text/html"})) as c:
        r, body, _ = wf._get(c, "https://example.com/x", wf.MAX_HTML_BYTES, resolver)
    assert len(body) == wf.MAX_HTML_BYTES


# ---------------------------------------------------------------- 本文の抽出

ARTICLE = """<html><head><title>人生管理OSの作り方 | サイト名</title>
<meta property="og:image" content="https://cdn.example.net/eye.png"><meta property="og:site_name" content="サイト名"></head>
<body><nav>メニュー ホーム 記事一覧</nav><article><h1>人生管理OSの作り方</h1>
<p>これは本文の最初の段落です。人生管理OSでは、Discord を入口にして記録を続けます。</p>
<p>二つ目の段落です。要約の材料になる内容がここにあります。さらに文章を続けて、抽出器が本文と判断できる長さにします。</p>
</article><footer>Copyright</footer></body></html>"""


def test_article_extraction_title_text_image():
    with client_for(lambda r: html_response(ARTICLE)) as c:
        art = wf.fetch_article("https://example.com/post", c, resolver)
    assert art.title.startswith("人生管理OSの作り方") and "本文の最初の段落" in art.text
    assert "メニュー" not in art.text and "Copyright" not in art.text  # ナビ・フッターは除く
    assert art.image_url == "https://cdn.example.net/eye.png" and art.kind == "web" and art.notes == []


def test_page_without_body_text_gets_a_note():
    with client_for(lambda r: html_response("<html><head><title>ログイン</title></head><body></body></html>")) as c:
        art = wf.fetch_article("https://example.com/private", c, resolver)
    assert art.text == "" and any("本文を取り出せません" in n for n in art.notes) and art.title == "ログイン"


def test_non_html_files_are_saved_as_url_only():
    with client_for(lambda r: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})) as c:
        art = wf.fetch_article("https://example.com/files/paper.pdf", c, resolver)
    assert art.kind == "file" and art.title == "paper.pdf" and art.text == ""
    assert "pdf" in art.notes[0] and "URL だけ" in art.notes[0]


# ---------------------------------------------------------------- X のポスト


def test_x_posts_use_the_public_api_not_the_login_page():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, content=json.dumps({"tweet": {
            "text": "こんにちは #テスト", "author": {"name": "太郎", "screen_name": "taro"},
            "media": {"photos": [{"url": "https://cdn.example.net/p.jpg"}]}}}).encode(), headers={"content-type": "application/json"})

    with client_for(handler) as c:
        art = wf.fetch_article("https://x.com/taro/status/123456", c, resolver)
    assert hosts == ["api.fxtwitter.com"]
    assert art.kind == "x" and art.title == "太郎（@taro）のポスト" and art.text == "こんにちは #テスト"
    assert art.image_url == "https://cdn.example.net/p.jpg" and art.final_url == "https://x.com/taro/status/123456"


def test_x_post_not_found_is_a_clear_error():
    with client_for(lambda r: httpx.Response(200, content=b'{"code":404,"message":"NOT_FOUND"}')) as c, pytest.raises(FetchError, match="X のポスト"):
        wf.fetch_article("https://twitter.com/taro/status/1", c, resolver)
    with client_for(lambda r: httpx.Response(200, content=b"not json")) as c, pytest.raises(FetchError, match="X のポスト"):
        wf.fetch_article("https://x.com/taro/status/1", c, resolver)


# ---------------------------------------------------------------- 画像


def test_fetch_image_ok_and_rejections():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    with client_for(lambda r: httpx.Response(200, content=png, headers={"content-type": "image/png"})) as c:
        assert wf.fetch_image("https://cdn.example.net/a.png", c, resolver) == (png, "image/png")
    with client_for(lambda r: html_response("<html></html>")) as c, pytest.raises(FetchError, match="画像ではありません"):
        wf.fetch_image("https://cdn.example.net/a.png", c, resolver)
    huge = b"0" * (wf.MAX_IMAGE_BYTES + 10)
    with client_for(lambda r: httpx.Response(200, content=huge, headers={"content-type": "image/jpeg"})) as c, pytest.raises(FetchError, match="大きすぎ"):
        wf.fetch_image("https://cdn.example.net/a.jpg", c, resolver)
    with client_for(lambda r: httpx.Response(302, headers={"location": "http://internal.test/x.png"})) as c, pytest.raises(FetchError, match="内部"):
        wf.fetch_image("https://cdn.example.net/a.png", c, resolver)


# ---------------------------------------------------------------- URL の抽出


def test_find_urls_strips_trailing_punctuation_and_dedupes():
    text = "これ見て https://example.com/a、あと(https://example.org/b?x=1#frag) と https://example.com/a。 #タグ"
    assert wf.find_urls(text) == ["https://example.com/a", "https://example.org/b?x=1#frag"]
    assert wf.find_urls("URLなし #デザイン") == []


def test_strip_urls_keeps_written_hashtags_only():
    assert "#frag" not in wf.strip_urls("https://example.com/a#frag #デザイン")
    assert "#デザイン" in wf.strip_urls("https://example.com/a#frag #デザイン")


def test_find_urls_in_japanese_text_and_parentheses():
    assert wf.find_urls("これ、https://example.com/a。すごい") == ["https://example.com/a"]
    assert wf.find_urls("https://en.wikipedia.org/wiki/Foo_(bar) です") == ["https://en.wikipedia.org/wiki/Foo_(bar)"]
    assert wf.find_urls("（https://example.com/x）と「https://example.org/y」") == ["https://example.com/x", "https://example.org/y"]
    assert wf.find_urls("https://ja.wikipedia.org/wiki/電子メール を保存") == ["https://ja.wikipedia.org/wiki/電子メール"]  # 日本語のまま貼った URL
    assert wf.find_urls("https://example.com/a?x=1&y=2, https://example.com/b.") == ["https://example.com/a?x=1&y=2", "https://example.com/b"]
