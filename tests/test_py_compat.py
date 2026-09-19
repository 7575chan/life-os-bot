"""古い Python（VM は 3.10〜3.12）で壊れるコードを、開発用の新しい Python（3.14）でも検出する。

Python 3.14 は型ヒントの評価を後回しにするため、次の問題が開発用のパソコンでは隠れる:
  クラスの中で `def list(...)` のように組み込みと同じ名前のメソッドを定義すると、その後に書いた
  `-> list[dict]` の `list` が、組み込みの list ではなくメソッドを指し、
  `TypeError: 'function' object is not subscriptable` で、モジュールを読み込む時点で失敗する。
  （`from __future__ import annotations` を書くか、別名を使えば防げる）
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "__pycache__", "vault", "data", ".git", ".pytest_cache"}


def project_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if not (SKIP_DIRS & set(p.relative_to(ROOT).parts))]


def has_future_annotations(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
               for n in tree.body)


def shadowed_annotation_violations(source: str) -> list[str]:
    """クラス内で先に定義された名前を、後のメソッドの型ヒント・既定値で使っている箇所（古い Python で壊れる）。"""
    tree = ast.parse(source)
    if has_future_annotations(tree):
        return []  # 型ヒントが実行時に評価されないので問題ない
    problems = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        defined: set[str] = set()
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                exprs = [a.annotation for a in item.args.args + item.args.kwonlyargs + item.args.posonlyargs if a.annotation]
                exprs += [x for x in (item.args.vararg and item.args.vararg.annotation,
                                      item.args.kwarg and item.args.kwarg.annotation, item.returns) if x]
                exprs += list(item.args.defaults) + [d for d in item.args.kw_defaults if d]
                for e in exprs:
                    for node in ast.walk(e):
                        if isinstance(node, ast.Name) and node.id in defined:
                            problems.append(f"{cls.name}.{item.name} (line {item.lineno}): '{node.id}' はクラス内で定義済み")
                defined.add(item.name)
            elif isinstance(item, ast.Assign):
                defined.update(t.id for t in item.targets if isinstance(t, ast.Name))
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                defined.add(item.target.id)
    return problems


def test_checker_detects_the_bug_that_broke_the_vm():
    buggy = "class Store:\n    def list(self, prefix: str) -> list[str]:\n        return []\n\n    def search(self) -> list[dict]:\n        return []\n"
    assert shadowed_annotation_violations(buggy)  # 修正前の notes.py と同じ形
    fixed = "from __future__ import annotations\n" + buggy
    assert shadowed_annotation_violations(fixed) == []
    ok = "class Store:\n    def list(self, prefix: str) -> 'list[str]':\n        return []\n"
    assert shadowed_annotation_violations(ok) == []  # 文字列の型ヒントは評価されない


def test_no_module_shadows_builtins_in_annotations():
    bad = {}
    for path in project_files():
        v = shadowed_annotation_violations(path.read_text(encoding="utf-8"))
        if v:
            bad[str(path.relative_to(ROOT))] = v
    assert not bad, bad


def test_all_files_parse_with_python_3_10_grammar():
    """VM が Ubuntu 22.04（Python 3.10）でも動くように、3.11 以降の構文（except* など）を使っていないこと。"""
    bad = []
    for path in project_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))
        except SyntaxError as e:
            bad.append(f"{path.relative_to(ROOT)}:{e.lineno} {e.msg}")
    assert not bad, bad


def test_notes_module_keeps_the_future_import():
    tree = ast.parse((ROOT / "notes.py").read_text(encoding="utf-8"))
    assert has_future_annotations(tree)
