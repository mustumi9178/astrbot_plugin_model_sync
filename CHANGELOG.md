# Changelog

本插件的所有重要变更都会记在这里。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 1.0.0

首个版本。

- 面板页面「模型同步」：下拉选目标模型、选改法、试运行、开始同步、还原上一次
- 扫描 AstrBot 自身配置里的 `provider_id` / `provider_name`（主对话、图片转述、上下文压缩）
- 扫描全部插件配置（`data/config/*_config.json`）里键名以 `provider_id` / `provider_name`
  结尾的字段，包括没在运行、只剩配置文件的孤儿插件
- 按用途分类：对话 / 看图 / 向量记忆 / 语音。向量记忆与语音归入保护组，永远不跟着换
- 只认真正的 provider 引用（已登记的 provider id，或 `<已登记的源>/<模型>`），
  `bge-m3`、`BAAI/bge-m3`、`FunAudioLLM/SenseVoiceSmall` 这类本地模型名不会被误判
- 目标模型不支持图片时，看图类引用跳过而不是改坏
- 两种改法：全部统一 / 只修坏的
- 写盘前把要改的文件整份备份到 `data/plugin_data/astrbot_plugin_model_sync/backups/<时间戳>/`，
  附 `manifest.json` 记录原路径；「还原上一次」把文件和内存里的值一起退回去
- 原子写盘（临时文件 + `os.replace`），保留原有的 UTF-8 BOM
- 改完自动重载被改动的插件
- 聊天指令 `/模型同步`、`/模型同步 执行`、`/模型同步 还原`（管理员）
