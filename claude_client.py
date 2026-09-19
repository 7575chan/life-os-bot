"""Claude API ラッパ。人格プロンプトと CEO 方針の注入をここに集約する。"""
import asyncio
import base64
import json
import time

from anthropic import AsyncAnthropic

import config
import notes
import util
import vault_paths

PERSONA = """あなたはユーザー専用の秘書兼「人生管理OS」です。黒衣（裏方）として静かに支えます。
【絶対のルール】
- ユーザーの機嫌と自己肯定感を最優先し、全肯定で接する。
- 指示・正論・説教・お節介なアドバイスはしない。「もっとこうすべき」は言わない。
- 減点評価をしない。「1文字でも進んだら100点」。未完了や記録の空白を責めず、事実として淡々と扱う。
- 返信は短く、装飾は控えめに。頼まれていない質問や深掘りをしない。
- 日本語で答える。"""

CEO_FILE = vault_paths.ceo_directives()  # 06-Life-OS/13-im-the-ceo/CEO-Directives.md
_CEO_TTL = 60
_ceo_cache: tuple[float, str] = (0.0, "")

_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def invalidate_ceo_cache() -> None:
    global _ceo_cache
    _ceo_cache = (0.0, "")


async def ceo_directives() -> str:
    global _ceo_cache
    ts, text = _ceo_cache
    if time.time() - ts < _CEO_TTL:
        return text
    try:
        text = await asyncio.to_thread(notes.get_store().read, CEO_FILE) or ""
    except Exception:
        text = ""
    _ceo_cache = (time.time(), text)
    return text


async def system_prompt(extra: str = "", use_ceo: bool = True) -> str:
    parts = [PERSONA]
    if use_ceo:
        ceo = (await ceo_directives()).strip()
        if ceo:
            parts.append("【CEO方針（最優先の判断基準。助言・優先順位づけ・分析はこれに従う）】\n" + ceo[:8000])
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def complete(prompt: str, *, extra_system: str = "", max_tokens: int = 1500, use_ceo: bool = True) -> str:
    resp = await client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=await system_prompt(extra_system, use_ceo),
        messages=[{"role": "user", "content": prompt}],
    )
    return _text(resp)


async def complete_json(prompt: str, fallback, *, max_tokens: int = 1500, use_ceo: bool = False):
    """JSON だけを返させる。解析失敗・API失敗時は fallback。"""
    try:
        text = await complete(
            prompt + "\n\n出力は有効なJSONのみ。説明文やコードフェンスは付けない。",
            max_tokens=max_tokens, use_ceo=use_ceo,
        )
    except Exception:
        return fallback
    data = util.extract_json(text)
    return fallback if data is None else data


async def vision_json(images: list[tuple[bytes, str]], prompt: str, fallback, *, max_tokens: int = 1200):
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
        for b, mt in images
    ]
    content.append({"type": "text", "text": prompt + "\n\n出力は有効なJSONのみ。説明文やコードフェンスは付けない。"})
    try:
        resp = await client().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=max_tokens,
            system=await system_prompt(use_ceo=False),
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        return fallback
    data = util.extract_json(_text(resp))
    return fallback if data is None else data


async def run_tools(prompt: str, tools: list[dict], execute, *, extra_system: str = "", max_iter: int = 8) -> str:
    """tool_use ループ。execute(name, input) は async で JSON 化可能な値を返す。"""
    messages = [{"role": "user", "content": prompt}]
    system = await system_prompt(extra_system)
    for _ in range(max_iter):
        resp = await client().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=2500, system=system, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            return _text(resp)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = await execute(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)})
            except Exception as e:  # noqa: BLE001 - モデルにエラーを見せて自己修正させる
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"{type(e).__name__}: {e}"})
        messages.append({"role": "user", "content": results})
    return "処理が長くなったので一旦ここまでにします。続きがあればもう一度送ってください。"
