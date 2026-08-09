"""Command line entry point.

`bilisama config show` expands every overlay and marks where each value came from.
It is the first thing to run when a config behaves unexpectedly.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bilisama import __version__
from bilisama.bootstrap import s2s_launch
from bilisama.config import Chattiness, ProviderName, Settings, check, derive, load

# Relative to the repo, not the working directory, so the CLI works from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = _REPO_ROOT / "config" / "bilisama.toml"


def _load(path: Path) -> Settings:
    """Read the config, reporting problems in plain language.

    Args:
        path: Path to the TOML file.

    Returns:
        A validated settings object.

    Raises:
        SystemExit: File missing or unreadable, malformed TOML, or failed validation.
            A streamer who sees a traceback just files a ticket, so we never show one.
    """
    if not path.exists():
        print(f"找不到配置文件：{path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return load(path)
    except (ValidationError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"配置有问题：\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_show(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    payload: dict[str, Any] = settings.model_dump(mode="json")
    # Called out separately so nobody assumes these can be set in the TOML.
    payload["_derived"] = {
        "source": "chattiness",
        **derive(settings.interaction.chattiness).model_dump(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    problems = check(settings)
    if not problems:
        print("配置没问题。")
        return 0
    for p in problems:
        tag = "错误" if p.fatal else "提醒"
        print(f"[{tag}] {p.field}：{p.message}")
        if p.fix:
            print(f"        怎么办：{p.fix}")
    return 1 if any(p.fatal for p in problems) else 0


def cmd_render_s2s(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    if settings.speech.provider is not ProviderName.S2S:
        print(
            f"当前语音后端是 {settings.speech.provider}，不需要渲染 s2s 启动配置。",
            file=sys.stderr,
        )
        return 2
    try:
        result = s2s_launch.write(settings.speech.s2s, args.out, s2s_root=args.s2s_root)
    except s2s_launch.S2SConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"已写入 {args.out}")
    if args.s2s_root is None:
        print("提示：没给 --s2s-root，跳过了跟上游字段名的对账。")
    elif result.missing_turn_fields:
        print(f"提醒：这些判停参数没有透传：{'、'.join(result.missing_turn_fields)}")
    return 0


def cmd_chattiness(args: argparse.Namespace) -> int:
    """Print the thresholds each chattiness level derives.

    Useful when answering "why is it talking this much?".
    """
    for level in Chattiness:
        d = derive(level)
        marker = " ←" if level.value == args.level else ""
        print(f"{level.value:>7}  {d.model_dump()}{marker}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bilisama", description="B 站直播 AI 伴播")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="配置相关")
    config_sub = config.add_subparsers(dest="config_command", required=True)

    for name, fn, helptext in (
        ("show", cmd_show, "展开所有覆盖层，打印最终生效值"),
        ("validate", cmd_validate, "只校验不启动"),
    ):
        p = config_sub.add_parser(name, help=helptext)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.set_defaults(func=fn)

    p_render = config_sub.add_parser("render-s2s", help="渲染 speech-to-speech 的启动 JSON")
    p_render.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_render.add_argument("--out", type=Path, default=Path("config/s2s/bilisama-s2s.json"))
    p_render.add_argument(
        "--s2s-root", type=Path, default=None, help="上游检出目录，用于字段名对账"
    )
    p_render.set_defaults(func=cmd_render_s2s)

    p_chat = config_sub.add_parser("chattiness", help="打印话痨度三档派生出的阈值")
    p_chat.add_argument("--level", default="medium")
    p_chat.set_defaults(func=cmd_chattiness)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
