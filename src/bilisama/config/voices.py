"""The vetted voice lists, one place instead of three.

Before this module the DashScope list lived only as a runbook table and the
volcano officials only as prose inside validate.py's error text. The picker
needs them as data, so this is now the single source: the runbook tables cite
it, validate.py's examples read from it, and a unit test keeps the runbook
text and these tuples telling the same story.
"""

from __future__ import annotations

__all__ = ["DASHSCOPE_VOICES", "VOLCANO_OFFICIAL_SPEAKERS"]

# DashScope's cosyvoice ids with their measured base pitch, low to high.
# All fifteen were probed with the same sentence on the real endpoint
# (2026-08, runbook 「换音色（DashScope）」); the Hz number is the whole hint a
# streamer needs — under ~260 Hz reads as an ordinary adult female voice,
# the server's own default (longanqian, 343 Hz) reads as shrill.
DASHSCOPE_VOICES: tuple[tuple[str, int], ...] = (
    ("longanlufeng", 150),
    ("loongjohn", 174),
    ("longpaopao_v3.6", 235),
    ("longanlingxin", 242),  # the shipped default
    ("longchuanshu_v3.6", 242),
    ("loongmary", 282),
    ("longanfengyue", 286),
    ("longanlingxi", 289),
    ("longhuohuo_v3.6", 296),
    ("longanhuan_v3.6", 304),
    ("longjielidou_v3.6", 308),
    ("longanyuanfei", 320),
    ("loongeva_v3.6", 333),
    ("longanqian", 343),  # the server default when the field is left empty
    ("longanxiaoxin", 393),
)

# Volcano O2.0 (1.2.1.1) official voices — the only ids that generation can
# take without the silent failure modes validate.py guards (a wrong pairing
# swaps her persona or mutes her without one error on the wire). SC2.0 takes
# cloned ids (saturn_/ICL_/S_ prefixes) which are account-specific, so there
# is no list to ship for it.
VOLCANO_OFFICIAL_SPEAKERS: tuple[str, ...] = (
    "zh_female_vv_jupiter_bigtts",
    "zh_female_xiaohe_jupiter_bigtts",
    "zh_male_yunzhou_jupiter_bigtts",
    "zh_male_xiaotian_jupiter_bigtts",
)
