"""The vetted voice lists, one place instead of three.

Before this module the DashScope list lived only as a runbook table and the
volcano officials only as prose inside validate.py's error text. The picker
needs them as data, so this is now the single source: the runbook tables cite
it, validate.py's examples read from it, and a unit test keeps the runbook
text and these tuples telling the same story.
"""

from __future__ import annotations

__all__ = ["DASHSCOPE_VOICES", "DUPLEX_FEATURED", "DUPLEX_VOICES", "VOLCANO_OFFICIAL_SPEAKERS"]

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

# Volcano full-duplex (Seeduplex, 1.2.6.1): the Chinese voices of the vendor's
# 「豆包语音合成模型2.0、S2S-O2.0、S2S-全双工」 table that made a sound when
# swept against the real endpoint on 2026-10-07, with the measured base pitch
# of one reply each. The endpoint accepts any id, and a wrong one creates the
# session, writes her text and sends no audio, with no error frame — so the
# picker offers only these, and validate.py warns about anything else that is
# not a cloned voice. DUPLEX_FEATURED are the four the vendor calls its main
# S2S voices (the only ones with dialect support); the shipped default is the
# first of them.
DUPLEX_FEATURED: tuple[str, ...] = (
    "zh_female_vv_uranus_bigtts",
    "zh_female_xiaohe_uranus_bigtts",
    "zh_male_m191_uranus_bigtts",
    "zh_male_taocheng_uranus_bigtts",
)
DUPLEX_VOICES: tuple[tuple[str, str, int], ...] = (
    # The vendor's four featured voices first, then the rest by pitch.
    ("zh_female_vv_uranus_bigtts", "Vivi 2.0", 255),
    ("zh_female_xiaohe_uranus_bigtts", "小何 2.0", 229),
    ("zh_male_m191_uranus_bigtts", "云舟 2.0", 133),
    ("zh_male_taocheng_uranus_bigtts", "小天 2.0", 150),
    ("zh_female_popo_uranus_bigtts", "婆婆 2.0", 190),
    ("zh_female_wenroumama_uranus_bigtts", "温柔妈妈 2.0", 203),
    ("zh_female_jitangnv_uranus_bigtts", "鸡汤女 2.0", 207),
    ("zh_female_lingling_uranus_bigtts", "玲玲姐姐 2.0", 213),
    ("zh_female_tianmeiyueyue_uranus_bigtts", "甜美悦悦 2.0", 214),
    ("zh_female_kailangjiejie_uranus_bigtts", "开朗姐姐 2.0", 216),
    ("zh_female_yingyujiaoxue_uranus_bigtts", "Tina老师 2.0", 217),
    ("zh_female_xinlingjitang_uranus_bigtts", "心灵鸡汤 2.0", 218),
    ("zh_female_wuzetian_uranus_bigtts", "武则天 2.0", 218),
    ("zh_female_cancan_uranus_bigtts", "知性灿灿 2.0", 222),
    ("zh_female_gujie_uranus_bigtts", "顾姐 2.0", 224),
    ("zh_female_nvleishen_uranus_bigtts", "女雷神 2.0", 226),
    ("zh_female_linjianvhai_uranus_bigtts", "邻家女孩 2.0", 229),
    ("zh_female_mizai_uranus_bigtts", "黑猫侦探社咪仔 2.0", 229),
    ("zh_female_kefunvsheng_uranus_bigtts", "暖阳女声 2.0", 231),
    ("zh_female_meilinvyou_uranus_bigtts", "魅力女友 2.0", 231),
    ("zh_female_wenjingmaomao_uranus_bigtts", "文静毛毛 2.0", 233),
    ("zh_female_sophie_uranus_bigtts", "魅力苏菲 2.0", 235),
    ("zh_female_gaolengyujie_uranus_bigtts", "高冷御姐 2.0", 238),
    ("zh_female_zhixingnv_uranus_bigtts", "知性女声 2.0", 238),
    ("zh_female_wenroushunv_uranus_bigtts", "温柔淑女 2.0", 239),
    ("zh_female_qingchezizi_uranus_bigtts", "清澈梓梓 2.0", 240),
    ("zh_female_jitangmei_uranus_bigtts", "鸡汤妹妹 2.0", 240),
    ("zh_female_qinqienv_uranus_bigtts", "亲切女声 2.0", 241),
    ("zh_female_liuchangnv_uranus_bigtts", "流畅女声 2.0", 242),
    ("zh_female_tianmeitaozi_uranus_bigtts", "甜美桃子 2.0", 244),
    ("zh_female_roumeinvyou_uranus_bigtts", "柔美女友 2.0", 245),
    ("zh_female_qingxinnvsheng_uranus_bigtts", "清新女声 2.0", 247),
    ("zh_female_tiexinnvsheng_uranus_bigtts", "贴心女声 2.0", 247),
    ("zh_female_jiaochuannv_uranus_bigtts", "娇喘女声 2.0", 251),
    ("zh_female_shaoergushi_uranus_bigtts", "少儿故事 2.0", 251),
    ("zh_female_shuangkuaisisi_uranus_bigtts", "爽快思思 2.0", 253),
    ("zh_female_xiaoxue_uranus_bigtts", "儿童绘本 2.0", 253),
    ("zh_female_tvbnv_uranus_bigtts", "TVB女声 2.0", 254),
    ("zh_female_peiqi_uranus_bigtts", "佩奇猪 2.0", 264),
    ("zh_female_chunribu_uranus_bigtts", "春日部姐姐 2.0", 264),
    ("zh_female_wenrouxiaoya_uranus_bigtts", "温柔小雅 2.0", 265),
    ("zh_female_yingtaowanzi_uranus_bigtts", "樱桃丸子 2.0", 270),
    ("zh_female_gufengshaoyu_uranus_bigtts", "古风少御 2.0", 270),
    ("zh_female_zhishuaiyingzi_uranus_bigtts", "直率英子 2.0", 279),
    ("zh_female_mengyatou_uranus_bigtts", "萌丫头 2.0", 279),
    ("zh_female_qiaopinv_uranus_bigtts", "俏皮女声 2.0", 282),
    ("zh_female_tianmeixiaoyuan_uranus_bigtts", "甜美小源 2.0", 286),
    ("zh_female_linxiao_uranus_bigtts", "林潇 2.0", 291),
    ("zh_female_sajiaoxuemei_uranus_bigtts", "撒娇学妹 2.0", 293),
    ("zh_female_chanmeinv_uranus_bigtts", "谄媚女声 2.0", 300),
    ("zh_female_ganmaodianyin_uranus_bigtts", "感冒电音姐姐 2.0", 320),
    ("zh_male_shenyeboke_uranus_bigtts", "深夜播客 2.0", 100),
    ("zh_male_aojiaobazong_uranus_bigtts", "傲娇霸总 2.0", 106),
    ("zh_male_liufei_uranus_bigtts", "刘飞 2.0", 107),
    ("zh_male_yuanboxiaoshu_uranus_bigtts", "渊博小叔 2.0", 107),
    ("zh_male_wennuanahu_uranus_bigtts", "温暖阿虎 2.0", 112),
    ("zh_male_kuailexiaodong_uranus_bigtts", "快乐小东 2.0", 112),
    ("zh_male_ruyayichen_uranus_bigtts", "儒雅逸辰 2.0", 114),
    ("zh_male_youyoujunzi_uranus_bigtts", "悠悠君子 2.0", 114),
    ("zh_male_gaolengchenwen_uranus_bigtts", "高冷沉稳 2.0", 117),
    ("zh_male_shaonianzixin_uranus_bigtts", "少年梓辛 2.0", 119),
    ("zh_male_baqiqingshu_uranus_bigtts", "霸气青叔 2.0", 119),
    ("zh_male_dongfanghaoran_uranus_bigtts", "东方浩然 2.0", 119),
    ("zh_male_qingcang_uranus_bigtts", "擎苍 2.0", 122),
    ("zh_male_tangseng_uranus_bigtts", "唐僧 2.0", 127),
    ("zh_male_silang_uranus_bigtts", "四郎 2.0", 128),
    ("zh_male_xuanyijieshuo_uranus_bigtts", "悬疑解说 2.0", 130),
    ("zh_male_cixingjieshuonan_uranus_bigtts", "磁性解说男声 2.0", 130),
    ("zh_male_kailangxuezhang_uranus_bigtts", "开朗学长 2.0", 142),
    ("zh_male_jieshuoxiaoming_uranus_bigtts", "解说小明 2.0", 148),
    ("zh_male_dayi_uranus_bigtts", "大壹 2.0", 149),
    ("zh_male_qingshuangnanda_uranus_bigtts", "清爽男大 2.0", 150),
    ("zh_male_fanjuanqingnian_uranus_bigtts", "反卷青年 2.0", 151),
    ("zh_male_kailangdidi_uranus_bigtts", "开朗弟弟 2.0", 162),
    ("zh_male_guanggaojieshuo_uranus_bigtts", "广告解说 2.0", 162),
    ("zh_male_ruyaqingnian_uranus_bigtts", "儒雅青年 2.0", 166),
    ("zh_male_zhuangzhou_uranus_bigtts", "庄周 2.0", 166),
    ("zh_male_yizhipiannan_uranus_bigtts", "译制片男 2.0", 168),
    ("zh_male_wenrouxiaoge_uranus_bigtts", "温柔小哥 2.0", 176),
    ("zh_male_zhubajie_uranus_bigtts", "猪八戒 2.0", 193),
    ("zh_male_huolixiaoge_uranus_bigtts", "活力小哥 2.0", 197),
    ("zh_male_yangguangqingnian_uranus_bigtts", "阳光青年 2.0", 209),
    ("zh_male_liangsangmengzai_uranus_bigtts", "亮嗓萌仔 2.0", 211),
    ("zh_male_linjiananhai_uranus_bigtts", "邻家男孩 2.0", 222),
    ("zh_male_xionger_uranus_bigtts", "熊二 2.0", 241),
    ("zh_male_lubanqihao_uranus_bigtts", "鲁班七号 2.0", 242),
    ("zh_male_sunwukong_uranus_bigtts", "猴哥 2.0", 247),
    ("zh_male_lanyinmianbao_uranus_bigtts", "懒音绵宝 2.0", 258),
    ("zh_male_tiancaitongsheng_uranus_bigtts", "天才童声 2.0", 261),
    ("zh_male_naiqimengwa_uranus_bigtts", "奶气萌娃 2.0", 312),
)
