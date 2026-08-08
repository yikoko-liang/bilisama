"""provider 能力位。

判据很硬：**一个字段如果只有 adapter 自己读，它就不是 capability，是 adapter 的常量。**
采样率、超时、会话上限、错误分类全部按这条降级到 adapter 里了。

上游 qwen-audio-agent 的 DEFAULT_CAPABILITIES 只有 3 个布尔，方言在独立的 codec
对象里、连接细节是 provider 上的普通函数。我们照这个分,把三样东西揉回一个
dataclass 是自造复杂度。

这里的六个字段都会让**客户端或调度器长出分支**，所以它们才配叫 capability。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Capabilities:
    # 它自己出音频，还是给我们文本让我们自己合成
    owns_tts: bool = True
    # 同时只能有一个回复在生成
    single_response_slot: bool = False
    # 旁路回复（conversation="none"）占不占那唯一的名额
    out_of_band_exempt_from_slot: bool = False
    # 支不支持 conversation.item.truncate。不支持就只能在我们自己的记忆层截断
    item_truncate: bool = False
    # 会不会回 session.updated。不会的话客户端不能等这个 ack
    acknowledges_session_update: bool = True
    # 声明支持哪些判停类型。配了不支持的直接报错，不静默降级
    turn_detection_types: frozenset[str] = field(default_factory=lambda: frozenset({"server_vad"}))

    @property
    def expr_tags_safe(self) -> bool:
        """内联 <expr/> 标签安不安全,恒等于「不是它做 TTS」。

        存成字段就会有两处真相，所以做成派生属性。
        """
        return not self.owns_tts


# 三份 profile。方言不在这里，在 codec 里。

S2S = Capabilities(
    owns_tts=False,  # 我们请求纯文本，自己合成
    single_response_slot=True,
    out_of_band_exempt_from_slot=False,  # 已核实：in_response 检查在 is_out_of_band 之前
    item_truncate=False,  # 上游没实现
    acknowledges_session_update=True,
    turn_detection_types=frozenset({"server_vad"}),  # semantic_vad 会被收下然后忽略
)

DASHSCOPE = Capabilities(
    owns_tts=True,
    single_response_slot=False,  # 待实测，见计划 §13
    out_of_band_exempt_from_slot=False,  # 待实测
    item_truncate=False,  # 待实测
    acknowledges_session_update=True,
    turn_detection_types=frozenset({"smart_turn", "server_vad", "semantic_vad"}),
)

OPENAI_GA = Capabilities(
    owns_tts=True,
    single_response_slot=True,  # 只能有一个写主对话
    out_of_band_exempt_from_slot=True,  # 旁路回复可以并发
    item_truncate=True,  # WebSocket 下这是必需的，它支持
    acknowledges_session_update=True,
    turn_detection_types=frozenset({"server_vad", "semantic_vad"}),
)
