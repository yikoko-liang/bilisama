# Live2D 的商用门槛

BiliSama 的形象层规划用 Live2D。这件事真正的坎不是找模型资源，是 SDK 授权——它有
审核和签约周期，属于日历时间，不是工程时间。这份文档写清楚对外发布之前必须先满足
什么，以及现在处在哪一步。

**当前状态（2026-08-23）**：仓库里没有任何 Live2D 资源，也没有 Cubism Core。形象层
今天跑的是精灵图桌宠（内置豆腐机器人）。下面这些条件一条都还没开始走。

## 内部开发可以用，对外发布不行

用官方 sample 模型做内部开发和 demo 是允许的，属于大型企业条款里的 Internal /
Supervision 用途。但对外商用发布之前，下面五件必须先办完。

### 一、先提交 Expandable Application 申请并获批

Live2D 专有 EULA §1.5 把 `avatars, live streaming applications` 明确写进了
Expandable Application 的定义，§2.1 要求发布前先申请、先过审，而且**没有任何规模
豁免**。这不是自助流程，有审核和签约周期，所以该早发邮件早发。

### 二、确认发布主体的规模档

如果发布主体是哔哩哔哩（年营收 ≥1 亿日元，属 Large-Scale），**全部官方 sample 模型
都不可商用**，必须自购或定制模型。Expandable 的费率起谈价是初始 ¥300,000，外加年费
¥1,200,000／平台，再加 5%。

### 三、问清楚"免费应用"这个真空地带

官网写着 *"as a general rule, fully free of charge is not eligible for approval"*
——完全免费的应用原则上不予批准。如果 BiliSama 是免费的内部工具，可能既不满足付费
审批的条件、又走不了常规豁免。这一条要书面问，不要靠猜。

### 四、EULA 里的声明段和露出义务

嵌入 Live2D 指定的 2000 万日元声明段，展示 Live2D logo，登上 Showcase 页。

### 五、许可文件分开放

根 LICENSE 末尾注明 "except for the Live2D assets, which are governed by
LICENSE-Live2D.md"，单独放一份 `LICENSE-Live2D.md`。**从官网拉 v1.7 现行版**，不要
复制 Open-LLM-VTuber 仓库里那份过期的 v1.6。

## 一个可能省掉这笔费用的产品选择

如果只内置一个自有的固定模型、完全不提供"加载你自己的 .moc3"，那就不构成
Expandable Application——§1.5 的判定标准是 "generates any indefinite number of
models"。

但这跟"主播用自己皮套"这个卖点冲突，得由产品负责人来定，工程侧不能默认按这条走。
这也是计划 §13 第 6 件要问产品和法务的两个问题之一。

## 免费公模为什么救不了场

免费公模确实很多（BOOTH 上有 1682 件，模之屋也有一大堆），但它们普遍禁止再分发
模型文件，而 Electron 内置模型恰恰就是再分发。所以"下个免费公模就开工"这条路走不通。

## 工程侧的对冲

形象层封在 `AvatarRenderer` 接口后面（`load` / `setExpression` / `playMotion` /
`setMouth`），Live2D 只是它的一个实现。今天在跑的精灵图桌宠走的就是同一个接口。
真要是商务谈不下来，加一个 PNGTuber 实现是前端一两天的事，P2 一行都不用改。

## 一个反面教材

不要照抄 my-neuro 的做法。它把官方 Hiyori 的骨骼换皮成自有角色"肥牛"（moc3 体积
几乎相同，动作文件名还叫 `Hiyori_m01.motion3.json`），整体盖 MIT，仓库里没有任何
Live2D 声明。这同时踩了"Hiyori 不许改设计"和"不得以非 Live2D 许可发布"两条，对方
可以单方面终止授权并要求销毁全部衍生物。

## Cubism Core 怎么拿

它不在 npm 上，必须自己 vendor：从 live2d.com 下载 `CubismSdkForWeb-5-r.x.zip`，
取出 `Core/live2dcubismcore.min.js`。它是 EULA 定义的 "Redistributable Code"，可以
随应用原样分发，但**必须保留文件头那 8 行版权注释**（§4.1.7 明确禁止移除）。

不要在运行时热链官网那个 URL——应用要能离线跑，而且那个 "Latest" 地址没有版本号。
做法是 `scripts/fetch-live2d-core.mjs` 在 postinstall 阶段下载到 gitignored 的
`desktop/resources/vendor/live2d/`，SDK 的 release 号固定写在 package.json 里。

出处：计划文档 §6.4，以及 NOTICE 里 Live2D Cubism Core 那一条。
