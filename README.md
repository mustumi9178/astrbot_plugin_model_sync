# 模型一键同步 · astrbot_plugin_model_sync

> 换完对话模型点一下，把还指着旧模型的地方全改过去。

AstrBot 的 provider id 形如 `<源 id>/<模型 id>`。模型那一项一关，它就从
`provider_manager` 的 `inst_map` 里悄悄消失；所有还钉在它上面的地方都会静默掉到
`provider_insts[0]` 上，日志里只留一行：

```
[WARN] [star.context:481]: Provider <id> was not found. Its prov...
```

界面照常能用，人也看不出来。真出问题时你只会看到一句 `All chat models failed`，然后
开始一个个翻插件配置。

这个插件就是把这一摊显式收拾掉：换完模型点一下，所有还钉在旧模型上的地方一次改到
新模型上。

## 它改哪些地方

- **AstrBot 自己**：主对话模型（`agent_runner.config.model.provider_id`）、
  图片转述（`provider_settings.default_image_caption_provider_id`）、
  上下文压缩（`agent_runner.config.compression.provider_id`）
- **所有插件配置**（`data/config/*_config.json`）里键名以 `provider_id` /
  `provider_name` 结尾的字段。比如 `model.provider_id`、`llm.reply_provider_id`、
  `provider_settings.vision_provider_id`、`hf_judge_provider_name`
- **没在运行、只剩配置文件的插件**也会被扫到（只改文件，不重载）

## 它不碰什么

- **向量记忆**（`embedding_provider_id`）、**语音**（`tts` / `stt` / `asr` 相关的
  `provider_id`）—— 按用途归到保护组，永远不跟着换。它们本来就不该指向对话模型
- **不是 provider 引用的字符串**。值必须真的是 AstrBot 里登记过的 provider id，或者
  `<已登记的源>/<模型>`。`bge-m3`、`BAAI/bge-m3`、`FunAudioLLM/SenseVoiceSmall`、
  `bs_roformer_ep_317_sdr_12.9755` 这种本地模型名一个都不会被误判
- `provider_settings.websearch_provider` 这一类叶子键名对不上的字段一律跳过
  （它存的是 provider **源** 的 id，不是一个模型）
- 目标模型不支持图片时，看图类的引用跳过而不是改坏

## 怎么用

### 面板

`插件管理` → `模型一键同步` → 详情里点 **打开页面**。

1. 在「要全部指向哪个模型」里选新模型（只列对话模型，关着的会标出来，能做图的标「能看图」）
2. 选改法：**全部统一** / **只修坏的**（后者只动指向已关闭或不存在模型的那些）
3. 先点 **试运行** 看一遍能不能改（不落盘、不备份、不重载）
4. 再点 **开始同步** —— 先备份，再写盘，写完自动重载受影响的插件
5. 反悔了就点 **还原上一次**

### 聊天指令（管理员）

| 指令 | 作用 |
| --- | --- |
| `/模型同步` | 只列清单，不改 |
| `/模型同步 执行` | 真的改，改前自动备份，改完重载被改的插件 |
| `/模型同步 还原` | 回到上一次备份 |

## 安装

**插件市场**：上架后可在 `插件管理` → 插件市场里搜「模型一键同步」。

**从仓库安装**：`插件管理` → 安装插件 → 填仓库地址

```
https://github.com/mustumi9178/astrbot_plugin_model_sync
```

**手动**：把整个目录放进 `data/plugins/`，然后重启 AstrBot 或者在面板里点一次重载。

> `ASTRBOT_RELOAD` 没设成 `1` 的时候，AstrBot 不会自己发现新丢进去的目录，需要重启或手动重载。

要求：AstrBot `>= 4.20.0`。没有任何第三方 Python 依赖。

## 安全网

- 动手前把要改的文件整份复制到
  `data/plugin_data/astrbot_plugin_model_sync/backups/<时间戳>/`，附一份 `manifest.json`
  记着原路径。`还原上一次` 会把**文件和内存里的值**一起退回去，不用重启
- 写盘用临时文件 + `os.replace` 原子替换，保留原来的 UTF-8 BOM，不会写出半个 JSON
- 目标必须是**已启用**的对话模型；指到向量或语音上会被挡住
- 每次同步的失败项会逐条列出来，不会假装成功

## 顺序提醒

换模型时正确的顺序是：

**先把新模型加进来并启用 → 再点同步把所有引用改过去 → 最后才去关旧模型。**

反过来做，中间那段时间所有依赖都在静默回退到模型列表的第一个，界面一点异常都不给你看。

## 已知限制

- 只认键名以 `provider_id` / `provider_name` 结尾的字段。某个插件要是把模型 id 存在
  别的字段名下（比如 `default_model`），这里扫不到，得手动改
- 值的形态必须是 `<源>/<模型>`。纯模型名（不带源前缀）不会被当成 provider 引用
- 插件配置里如果这个键压根不存在（只是 schema 默认值），会跳过并报告，不会硬塞进去
- 备份只保留最近一次，多点几次同步会被新的覆盖（旧的目录还在，只是「还原上一次」认最新的那个）

## 许可

[MIT](LICENSE)
