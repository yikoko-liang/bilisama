"""Command line entry point.

`bilisama config show` expands every overlay and prints the values that actually
took effect. It is the first thing to run when a config behaves unexpectedly.

Plan §7.7 also asks it to tag each value with the layer it came from. That part is
not built: `loader.load` collapses the layers as it merges them, so nothing here
knows which one won.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from bilisama import __version__
from bilisama.bootstrap import s2s_launch
from bilisama.config import (
    Chattiness,
    ConfigError,
    ConfigProblem,
    ProviderName,
    Settings,
    check,
    derive,
    load,
)

# Relative to the repo, not the working directory, so the CLI works from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = _REPO_ROOT / "config" / "bilisama.toml"


# The two schema errors everyone actually hits: a typo'd key and a deleted
# required one. Anything else falls through to pydantic's English message,
# which at least now arrives with its field path in front (D7).
_FIELD_ERROR_TEXT = {
    "extra_forbidden": "不认识这个字段，多半是拼错了。对照 `bilisama config show` 的字段名改过来。",
    "missing": "缺了这个必填字段。",
}


def _report_validation(exc: ValidationError, *, stream: TextIO | None = None) -> None:
    """Print schema errors as field path + plain Chinese, one per line.

    Plan §7.6: a streamer who sees a raw `pydantic.ValidationError` just files
    a ticket.
    """
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "（顶层）"
        detail = _FIELD_ERROR_TEXT.get(err["type"], err["msg"])
        print(f"[错误] {loc}：{detail}", file=stream)


def _report(problems: list[ConfigProblem], *, stream: TextIO | None = None) -> None:
    """Print problems the way plan §7.6 promises: which field, what is wrong, what to do.

    Args:
        problems: What `check` found.
        stream: Where to write. Defaults to stdout.
    """
    for p in problems:
        tag = "错误" if p.fatal else "提醒"
        print(f"[{tag}] {p.field}：{p.message}", file=stream)
        if p.fix:
            print(f"        怎么办：{p.fix}", file=stream)


def _load(path: Path, *, strict: bool = True) -> Settings:
    """Read the config, reporting problems in plain language.

    Args:
        path: Path to the TOML file.
        strict: Refuse a config with a fatal problem. Off only for commands whose
            whole job is to describe a config that cannot start.

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
        return load(path, strict=strict)
    except ConfigError as exc:
        print("配置有问题，没法启动：", file=sys.stderr)
        _report(exc.problems, stream=sys.stderr)
        raise SystemExit(2) from exc
    except ValidationError as exc:
        print("配置有问题，没法启动：", file=sys.stderr)
        _report_validation(exc, stream=sys.stderr)
        raise SystemExit(2) from exc
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"配置有问题：\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats with null.

    JSON has no infinity, and the settings page reads this output (plan §7.5).
    `max_speech_ms` defaults to inf, and the launch renderer says the same thing by
    dropping the key, because upstream reads a missing value as "no limit"
    (bootstrap/s2s_launch.py:90-92). Here every field path has to stay put for the
    settings page to find it, so null carries that meaning instead.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def cmd_show(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    payload: dict[str, Any] = settings.model_dump(mode="json")
    # Called out separately so nobody assumes these can be set in the TOML.
    payload["_derived"] = {
        "source": "chattiness",
        **derive(settings.interaction.chattiness).model_dump(),
    }
    # allow_nan=False so a future non-finite default fails here instead of printing
    # Infinity, which no JSON parser outside Python accepts.
    print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    # This command exists to describe a broken config, so it has to be handed one.
    settings = _load(args.config, strict=False)
    problems = check(settings, config_dir=args.config.parent)
    if settings.speech.provider is not ProviderName.S2S:
        # Turn-type support lives in the provider registry, which config/ cannot
        # import (dependency direction), so the wiring sits here (D14). Lazy so
        # `config validate` does not pay for the realtime stack unless needed.
        from bilisama.realtime.providers import turn_type_problems

        hosted = getattr(settings.speech, settings.speech.provider.value)
        problems += turn_type_problems(settings.speech.provider, hosted.turn.type)
    if not problems:
        print("配置没问题。")
        return 0
    _report(problems)
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
    # Read the state off the result. The third state, UNAVAILABLE, never gets
    # here: s2s_launch.write refuses to write an unreconciled config, so it left
    # through the S2SConfigError arm above.
    if result.reconciliation is s2s_launch.Reconciliation.NOT_REQUESTED:
        print("提示：没给 --s2s-root，跳过了跟上游字段名的对账。")
        return 0
    print(f"已跟上游字段名对账：{args.s2s_root}")
    if result.missing_turn_fields:
        print(f"提醒：这些判停参数没有透传：{'、'.join(result.missing_turn_fields)}")
    return 0


def cmd_persona_list(args: argparse.Namespace) -> int:
    """Every persona the config dir ships, the active one marked. Switching
    is one line in the TOML (`[persona] id`) or a profile override."""
    from bilisama.persona.loader import default_data_dir

    settings = _load(args.config, strict=False)
    personas_dir = args.config.parent / "personas"
    if not personas_dir.is_dir():
        print(f"没有人设目录：{personas_dir}", file=sys.stderr)
        return 2
    for d in sorted(p for p in personas_dir.iterdir() if p.is_dir()):
        pid = d.name
        marker = "＊" if pid == settings.persona.id else "　"
        live_dir = (
            Path(settings.persona.data_dir).expanduser()
            if pid == settings.persona.id and settings.persona.data_dir != "auto"
            else default_data_dir(pid)
        )
        tags = []
        if (live_dir / "identity.md").is_file() or (live_dir / "personality.md").is_file():
            tags.append("锚有活副本")
        tags.append("专属话题提示词" if (d / "proactive.md").is_file() else "话题提示词用全局默认")
        for layer in ("relationship", "voice"):
            path = live_dir / f"{layer}.md"
            if path.is_file():
                count = sum(
                    1
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("- ")
                )
                tags.append(f"{layer} {count} 条")
        print(f"{marker} {pid:<8} {'、'.join(tags)}")
    print("切换：改 bilisama.toml 的 [persona] id，或 dev-talk --director --persona <id> 临时切。")
    return 0


def cmd_persona_review(args: argparse.Namespace) -> int:
    """The promotion gate from plan section 4.6: only a human moves an entry
    from a growth file into personality.md, and this command is that hand."""
    from bilisama.persona.loader import GrowthLayer, PersonaStore

    settings = _load(args.config)
    store = PersonaStore.from_config(settings.persona, config_dir=args.config.parent)

    layers: list[tuple[str, GrowthLayer, str]] = [
        ("r", "relationship", "共同经历"),
        ("v", "voice", "口癖样本"),
    ]
    entries: dict[str, tuple[GrowthLayer, str]] = {}
    for short, layer, label in layers:
        rows = store.growth_entries(layer)
        print(f"{label}（{layer}，{len(rows)} 条）")
        for i, row in enumerate(rows, start=1):
            ref = f"{short}{i}"
            entries[ref] = (layer, row)
            print(f"  [{ref}] {row}")
        if not rows:
            print("  （空）")

    def _pick(ref: str) -> tuple[GrowthLayer, str]:
        if ref not in entries:
            print(f"没有编号 {ref}，看上面的列表。", file=sys.stderr)
            raise SystemExit(2)
        return entries[ref]

    if args.promote:
        layer, entry = _pick(args.promote)
        try:
            store.promote(layer, entry)
        except ValueError as exc:
            # The distiller may rewrite the file between listing and clicking;
            # a stale pick is a retry, not a traceback (D11).
            print(f"没成：{exc}", file=sys.stderr)
            print("这条可能刚被蒸馏改写了，重新跑一遍看最新列表。", file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"已合并进 personality.md：{entry}")
    elif args.drop:
        layer, entry = _pick(args.drop)
        rows = store.growth_entries(layer)
        try:
            rows.remove(entry)
        except ValueError as exc:
            print(
                "这条已经不在生长层里了（可能刚被蒸馏改写），重新跑一遍看最新列表。",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        store.write_growth(layer, rows)
        print(f"已删掉：{entry}")
    else:
        print("用法：--promote r1 把那条合并进 personality.md；--drop v2 划掉不喜欢的。")
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

    persona = sub.add_parser("persona", help="人设相关")
    persona_sub = persona.add_subparsers(dest="persona_command", required=True)
    p_list = persona_sub.add_parser("list", help="列出可选人设，标出当前生效的")
    p_list.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_list.set_defaults(func=cmd_persona_list)
    p_review = persona_sub.add_parser(
        "review", help="翻生长层（共同经历/口癖），点头的合并进 personality.md"
    )
    p_review.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_review.add_argument("--promote", metavar="编号", help="把这条合并进 personality.md")
    p_review.add_argument("--drop", metavar="编号", help="把这条从生长层删掉")
    p_review.set_defaults(func=cmd_persona_review)

    # Registered lazily: dev-talk pulls in the realtime stack and possibly
    # sounddevice, none of which `config validate` should pay for.
    sub.add_parser("dev-talk", help="拿真人声音测语音链路（开发用）", add_help=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "dev-talk":
        from bilisama.dev_talk import main as dev_talk_main

        return dev_talk_main(raw[1:])
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
