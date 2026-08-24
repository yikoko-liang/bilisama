# kirby 皮肤包（仅限内部开发使用）

星之卡比（Kirby）角色版权归任天堂 / HAL Laboratory 所有。本目录内容：

- **只用于内部开发预览**，是开发者本人的桌宠形象；
- **不得随任何对外发布的产物分发**（安装包、镜像、公开仓库都算）；
- 2026-08-23 用户拍板：**这是内部预览版，本目录有意留在库内、也有意随包分发**，
  本轮不处理合规。原来这里写的是「正式化之前删除整个目录」，那条指令已被推翻——
  别再照它删。出厂默认早已回切豆腐（`config/bilisama.toml` 的 `[avatar]`
  是 `renderer = "tofu"`），所以默认体验不受影响，风险只在产物里；
- **对外发布前必须重新评估**（台账 #31）。届时的动作是从打包产物里排除本目录，
  而不是从仓库里删——「靠分支流程隔离」这条防线早就失效了：开发面已经从
  yiang/feat/ui 换到了 yiang/feat/packing。

刻意不进 NOTICE：NOTICE 是给可合规署名的开源移植用的，这份不是。

素材来源：shimejishop.com 的 Kirby shimeji（整理者 tornadotasmanian，
2026-08-14 下载），46 帧标准 shimeji 集，本包选用其中 17 个源帧（走路循环
另做左右翻转，成品共 20 格）。

重建方式（源帧不入库）：

```bash
python tools/skin_pack.py --frames-dir <shimeji帧目录> \
  --mapping src/bilisama/ui/web/skins/kirby/build.json \
  --out src/bilisama/ui/web/skins/kirby
```

`build.json` 是帧到十一条动画轨道的映射（idle 站立眨眼、waiting 坐姿听、
review 撅嘴思考、waving 挥手说话、jumping 戳一戳的跳起→拍扁→挥手、
failed 打瞌睡、走路循环左右翻转复用）。
