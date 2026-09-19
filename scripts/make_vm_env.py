"""VM に置く `.env` を、パソコンの `.env` から作る。値は表示しない。

    .venv/Scripts/python scripts/make_vm_env.py

`deploy/vm.env` ができる（Git には入らない）。開発用パソコン専用の設定（USE_TRUSTSTORE）を取り除き、
VM に必要な項目が入力済みかどうかを、項目名だけで報告する。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / ".env", ROOT / "deploy" / "vm.env"

PC_ONLY = {"USE_TRUSTSTORE", "OBSIDIAN_VAULT_DIR"}  # 開発用パソコン専用（VM には不要）
REQUIRED = ["DISCORD_TOKEN", "ANTHROPIC_API_KEY", "GOOGLE_SHEET_ID", "WORDCOUNT_SHEET_ID", "GAS_RELAY_URL",
            "GAS_RELAY_TOKEN", "OBSIDIAN_VAULT_FOLDER_ID", "OBSIDIAN_LIFEOS_FOLDER_ID", "OBSIDIAN_TASKFILE_ID"]


def convert(text: str) -> tuple[str, dict[str, str], list[str]]:
    """(VM 用の本文, 項目名->値, 取り除いた項目名)。"""
    out, values, removed = [], {}, []
    for line in text.splitlines():
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) in PC_ONLY:
            removed.append(m.group(1))
            continue
        if m and not line.lstrip().startswith("#"):
            values[m.group(1)] = m.group(2).strip()
        out.append(line)
    return "\n".join(out) + "\n", values, removed


def main() -> int:
    if not SRC.exists():
        print(".env が見つかりません")
        return 1
    body, values, removed = convert(SRC.read_text(encoding="utf-8"))
    if values.get("OBSIDIAN_BACKEND", "drive") != "drive":
        print("警告: OBSIDIAN_BACKEND が drive ではありません。VM では drive を使います。")
    DST.parent.mkdir(exist_ok=True)
    DST.write_text(body, encoding="utf-8", newline="\n")  # VM（Linux）用に改行は LF
    missing = [k for k in REQUIRED if not values.get(k)]
    print(f"作成しました: {DST.relative_to(ROOT)}")
    print("取り除いた開発用の項目:", ", ".join(removed) or "なし")
    if missing:
        print("★未入力の項目:", ", ".join(missing))
        return 1
    print("必要な項目はすべて入力済みです")
    return 0


if __name__ == "__main__":
    sys.exit(main())
