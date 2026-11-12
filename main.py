"""大肥鱼 · 模型一键同步

换了对话模型以后，把还钉在旧模型上的地方一次性改过来。

用法
- 面板：插件管理 → 模型一键同步 → 打开页面 → 点「开始同步」
- 聊天：/模型同步          （只预览，不改）
        /模型同步 执行      （预览 + 真的改 + 重载插件）
        /模型同步 还原      （回到上一次备份）

为什么要有这东西
AstrBot 的 provider id 形如 ``<源 id>/<模型 id>``。模型那一项一关，它就从
provider_manager 的 inst_map 里消失；所有还钉在它上面的地方（AstrBot 自己的
对话模型、看图转述、压缩模型，以及各插件配置里的 provider_id）都会静默掉到
provider_insts[0] 上，日志里只有一行 WARN。界面照常能用，人也不容易发现。
这里就是专门收拾这一摊。

三条硬规矩
- 只认 provider 引用：值必须是 AstrBot 里登记过的 provider id，或者是
  ``<已登记的源>/<随便什么>``。bge-m3、FunAudioLLM/SenseVoiceSmall、
  bs_roformer_ep_317 这类本地模型名一个都不碰。
- 向量(embedding)、语音(tts/stt/asr)按用途归到保护组，永远不改 —— 它们本来
  就不该指向对话模型。
- 动手前把要改的文件整份备份，改完立刻重载被改的插件。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import request
from astrbot.core.config import AstrBotConfig
from astrbot.core.star.star import star_registry
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_data_path,
)

PLUGIN_NAME = "astrbot_plugin_model_sync"
PREFIX = "/" + PLUGIN_NAME


def backup_root() -> str:
    """备份目录。做成函数：AstrBot 的 data 路径取决于启动时的工作目录。"""
    return os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME, "backups")

# ---------------------------------------------------------------- 分类 ----

CHAT = "chat"
VISION = "vision"
EMBEDDING = "embedding"
AUDIO = "audio"
PROTECTED = (EMBEDDING, AUDIO)

KIND_ZH = {CHAT: "对话", VISION: "看图", EMBEDDING: "向量记忆", AUDIO: "语音"}

SET = "set"
SAME = "same"
SKIP = "skip"

_HINTS = (
    (EMBEDDING, ("embedding", "embed_", "rerank")),
    (AUDIO, ("tts", "stt", "asr", "speech", "voice", "audio", "whisper")),
    (VISION, ("vision", "image", "caption", "visual", "ocr")),
)

# 叶子键名：provider_id / provider_name（允许任意前缀，如 vision_provider_id）
_LEAF_RE = re.compile(r"^(?:[A-Za-z0-9]+_)*(?:provider_id|provider_name)$")

CORE_LABELS = {
    "agent_runner.config.model.provider_id": "主对话模型",
    "provider_settings.default_image_caption_provider_id": "图片转述模型",
    "agent_runner.config.compression.provider_id": "上下文压缩模型",
    "provider_tts_settings.provider_id": "语音输出",
    "provider_stt_settings.provider_id": "语音识别",
}


def classify(dotted: str) -> str:
    low = dotted.lower()
    for kind, hints in _HINTS:
        if any(h in low for h in hints):
            return kind
    return CHAT


def _leaf(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _leaves(node, prefix, out):
    """收集所有 (点号路径, 字符串值)。list 整枝跳过——已知的钉子都是标量。"""
    if isinstance(node, dict):
        for key, value in node.items():
            _leaves(value, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(node, str):
        out.append((prefix, node))


# ------------------------------------------------------------- 模型清单 ----


class Inventory:
    """从 AstrBot 配置里读出 provider 清单和它们的用途。"""

    def __init__(self, cfg: dict) -> None:
        self.providers: dict[str, dict] = {}
        self.order: list[str] = []
        for item in cfg.get("provider") or []:
            if isinstance(item, dict) and item.get("id"):
                self.providers[item["id"]] = item
                self.order.append(item["id"])
        self.sources: dict[str, dict] = {}
        for item in cfg.get("provider_sources") or []:
            if isinstance(item, dict) and item.get("id"):
                self.sources[item["id"]] = item

    # -- 查询 ----------------------------------------------------------
    def kind(self, pid: str) -> str:
        item = self.providers.get(pid)
        if item:
            own = item.get("provider_type")
            if own:
                return str(own)
            src = self.sources.get(item.get("provider_source_id") or "")
            if src and src.get("provider_type"):
                return str(src["provider_type"])
            return "chat_completion"
        src_id = (pid or "").split("/", 1)[0]
        src = self.sources.get(src_id)
        if src and src.get("provider_type"):
            return str(src["provider_type"])
        return ""

    def exists(self, pid: str) -> bool:
        return bool(pid) and pid in self.providers

    def enabled(self, pid: str) -> bool:
        item = self.providers.get(pid)
        return bool(item and item.get("enable"))

    def is_ref(self, value: str) -> bool:
        """这个字符串看起来像不像一个 provider 引用。"""
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text:
            return False
        if text in self.providers:
            return True
        if "/" in text and text.split("/", 1)[0] in self.sources:
            return True
        return False

    def chat_ids(self) -> list[str]:
        return [pid for pid in self.order if self.kind(pid) == "chat_completion"]

    def enabled_chat(self) -> list[str]:
        return [pid for pid in self.chat_ids() if self.enabled(pid)]

    def supports_image(self, pid: str) -> bool:
        item = self.providers.get(pid) or {}
        return "image" in [str(m) for m in (item.get("modalities") or [])]

    def describe(self) -> list[dict]:
        out = []
        for pid in self.chat_ids():
            out.append(
                {
                    "id": pid,
                    "enable": self.enabled(pid),
                    "image": self.supports_image(pid),
                }
            )
        return out


# --------------------------------------------------------------- 钉子 ----


@dataclass
class Pin:
    scope: str
    path: str
    label: str
    kind: str
    before: str
    after: str = ""
    action: str = SAME
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "path": self.path,
            "label": self.label,
            "kind": self.kind,
            "kindZh": KIND_ZH.get(self.kind, self.kind),
            "before": self.before,
            "after": self.after,
            "action": self.action,
            "reason": self.reason,
        }


def scan_core(cfg: dict, inv: Inventory) -> list[Pin]:
    leaves: list[tuple[str, str]] = []
    _leaves(cfg, "", leaves)
    pins = []
    for path, value in leaves:
        if not _LEAF_RE.match(_leaf(path)):
            continue
        if not inv.is_ref(value):
            continue
        pins.append(
            Pin(
                scope="AstrBot",
                path=path,
                label=CORE_LABELS.get(path, path),
                kind=classify(path),
                before=value.strip(),
            )
        )
    return pins


def scan_plugin(scope: str, display: str, data: dict, inv: Inventory) -> list[Pin]:
    leaves: list[tuple[str, str]] = []
    _leaves(data, "", leaves)
    pins = []
    for path, value in leaves:
        if not _LEAF_RE.match(_leaf(path)):
            continue
        if not inv.is_ref(value):
            continue
        pins.append(
            Pin(
                scope=scope,
                path=path,
                label=f"{display} · {path}",
                kind=classify(path),
                before=value.strip(),
            )
        )
    return pins


def plan(pins: list[Pin], target: str, mode: str, inv: Inventory) -> list[Pin]:
    """mode: all = 全部统一；fix = 只修坏的。"""
    target_image = inv.supports_image(target)
    out: list[Pin] = []
    for pin in pins:
        row = dc_replace(pin)
        before = pin.before
        if pin.kind in PROTECTED:
            row.action, row.reason = SKIP, "向量/语音专用，不跟着换"
        elif before == target:
            row.action, row.reason = SAME, "已经指向目标"
        elif not before:
            row.action, row.reason = SKIP, "空着，保持不动"
        elif pin.kind == VISION and not target_image:
            row.action, row.reason = SKIP, "目标模型没有图片能力"
        else:
            broken = (not inv.exists(before)) or (not inv.enabled(before))
            if mode == "fix" and not broken:
                row.action, row.reason = SKIP, "还指着能用的模型，修坏模式下不动"
            else:
                row.action = SET
                row.after = target
                if before and not inv.exists(before):
                    row.reason = "指向 AstrBot 里没有的模型"
                elif before and not inv.enabled(before):
                    row.reason = "指向已关闭的模型"
                else:
                    row.reason = "统一到目标模型"
        out.append(row)
    return out


# --------------------------------------------------------------- 文件 ----


def load_json(path: str):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def dump_json(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = path + ".modelsync.tmp"
    with open(temp, "w", encoding="utf-8-sig", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _replace_in_place(target: dict, fresh: dict) -> None:
    """把 dict 的内容整体换掉，但保住对象本身——插件手里攥着的是同一个引用。"""
    try:
        target.clear()
        target.update(fresh)
    except Exception:  # noqa: BLE001
        for key in list(target.keys()):
            target.pop(key, None)
        target.update(fresh)


def set_path(root: dict, dotted: str, value) -> bool:
    parts = dotted.split(".")
    cursor = root
    for part in parts[:-1]:
        cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if not isinstance(cursor, dict):
            return False
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False
    cursor[parts[-1]] = value
    return True


def backup_files(files: list[str]) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_root(), stamp)
    os.makedirs(dest, exist_ok=True)
    manifest: dict[str, str] = {}
    for index, source in enumerate(files):
        if not source or not os.path.exists(source):
            continue
        target = f"{index:02d}__{os.path.basename(source)}"
        shutil.copy2(source, os.path.join(dest, target))
        manifest[target] = source
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return dest


def last_backup() -> str | None:
    root = backup_root()
    if not os.path.isdir(root):
        return None
    stamps = sorted(
        (name for name in os.listdir(root)
         if os.path.isdir(os.path.join(root, name))),
        reverse=True,
    )
    return stamps[0] if stamps else None


# -------------------------------------------------------------- 插件 ----


class ModelSyncPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._routes_backup = list(self.context.registered_web_apis)
        try:
            self.context.register_web_api(
                f"{PREFIX}/scan", self.api_scan, ["GET"], "扫描全部模型引用"
            )
            self.context.register_web_api(
                f"{PREFIX}/apply", self.api_apply, ["POST"], "一键同步模型引用"
            )
            self.context.register_web_api(
                f"{PREFIX}/restore", self.api_restore, ["POST"], "还原上一次同步"
            )
        except Exception:  # noqa: BLE001
            logger.exception("[模型同步] 注册 Web API 失败，面板页面会打不开")

    async def terminate(self) -> None:
        try:
            self.context.registered_web_apis[:] = self._routes_backup
        except Exception:  # noqa: BLE001
            logger.exception("[模型同步] 卸载时回收 Web API 失败")

    # ------------------------------------------------------- 扫描 ----

    def _core_cfg(self) -> AstrBotConfig:
        return self.context.get_config()

    def _plugin_targets(self) -> list[tuple[str, str, dict, str | None, str | None]]:
        """返回 (目录名, 显示名, 配置 dict, 已加载的 AstrBotConfig, 配置文件路径)。"""
        config_dir = get_astrbot_config_path()
        known: dict[str, tuple] = {}
        for meta in star_registry:
            root = getattr(meta, "root_dir_name", None)
            if not root:
                continue
            live = getattr(meta, "config", None)
            path = os.path.join(config_dir, f"{root}_config.json")
            data = live if isinstance(live, dict) else None
            if data is None and os.path.exists(path):
                try:
                    data = load_json(path)
                except Exception:  # noqa: BLE001
                    continue
            if not isinstance(data, dict):
                continue
            known[root] = (
                root,
                getattr(meta, "display_name", None) or getattr(meta, "name", None) or root,
                data,
                live if isinstance(live, dict) else None,
                path,
            )

        # 配置文件在、插件没加载（没启用 / 没注册）的也扫，但不重载
        if os.path.isdir(config_dir):
            for name in os.listdir(config_dir):
                if not name.endswith("_config.json"):
                    continue
                scope = name[: -len("_config.json")]
                if scope in known or scope == PLUGIN_NAME:
                    continue
                path = os.path.join(config_dir, name)
                try:
                    data = load_json(path)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(data, dict):
                    known[scope] = (scope, scope, data, None, path)

        return list(known.values())

    def build_state(self, target: str = "", mode: str = "all") -> dict:
        cfg = self._core_cfg()
        inv = Inventory(cfg)
        current = (
            ((cfg.get("agent_runner") or {}).get("config") or {})
            .get("model", {})
            .get("provider_id")
            or ""
        )

        pins = scan_core(cfg, inv)
        for scope, display, data, _live, _path in self._plugin_targets():
            try:
                pins.extend(scan_plugin(scope, display, data, inv))
            except Exception:  # noqa: BLE001
                logger.exception("[模型同步] 扫描插件 %s 的配置失败", scope)

        if not target:
            enabled = inv.enabled_chat()
            if current and current in enabled:
                target = current
            elif len(enabled) == 1:
                target = enabled[0]
            elif enabled:
                target = enabled[0]

        rows = plan(pins, target, mode, inv) if target else []
        rows.sort(key=lambda row: (row.action != SET, row.scope != "AstrBot", row.label))

        return {
            "pluginName": PLUGIN_NAME,
            "current": current,
            "target": target,
            "mode": mode,
            "providers": inv.describe(),
            "enabledChat": inv.enabled_chat(),
            "rows": [row.as_dict() for row in rows],
            "pending": sum(1 for row in rows if row.action == SET),
            "lastBackup": last_backup(),
            "coreConfigPath": getattr(cfg, "config_path", ""),
        }

    # ------------------------------------------------------- 写入 ----

    def _registered_name(self, scope: str) -> str | None:
        """目录名 → star_manager 认的插件名。两个不一样时按名重载，别退化成全量重载。"""
        for meta in star_registry:
            if getattr(meta, "root_dir_name", None) == scope:
                name = getattr(meta, "name", None)
                if name:
                    return name
        return None

    async def _apply(self, target: str, mode: str, with_reload: bool,
                     dry_run: bool = False) -> dict:
        cfg = self._core_cfg()
        inv = Inventory(cfg)
        if not target or target not in inv.providers:
            return {"ok": False, "message": f"目标模型 {target or '(空)'} 不在 AstrBot 的模型列表里"}
        if inv.kind(target) != "chat_completion":
            return {"ok": False, "message": "目标必须是对话模型，别指到向量或语音上"}
        if not inv.enabled(target):
            return {"ok": False, "message": "目标模型现在是关着的，先到「模型提供商」里把它打开"}

        state = self.build_state(target, mode)
        wanted = [row for row in state["rows"] if row["action"] == SET]
        if not wanted:
            return {"ok": True, "changed": 0, "rows": state["rows"],
                    "message": "没有需要改的地方", "backup": "", "reloads": []}

        core_rows = [row for row in wanted if row["scope"] == "AstrBot"]
        plugin_rows: dict[str, list[dict]] = {}
        for row in wanted:
            if row["scope"] != "AstrBot":
                plugin_rows.setdefault(row["scope"], []).append(row)

        targets = self._plugin_targets()
        by_scope = {item[0]: item for item in targets}

        # 试运行：只走一遍「能不能写进去」的判断，不落盘、不备份、不重载
        if dry_run:
            reachable, unreachable = [], []
            for row in core_rows:
                reachable.append(f"AstrBot · {row['path']}")
            for scope, rows in plugin_rows.items():
                entry = by_scope.get(scope)
                if not entry:
                    unreachable.extend(f"{scope} · {row['path']}" for row in rows)
                    continue
                _scope, _display, data, live, path = entry
                probe = live if live is not None else data
                for row in rows:
                    ok = False
                    cursor = probe
                    parts = row["path"].split(".")
                    for part in parts[:-1]:
                        cursor = cursor.get(part) if isinstance(cursor, dict) else None
                    ok = isinstance(cursor, dict) and parts[-1] in cursor
                    (reachable if ok else unreachable).append(f"{scope} · {row['path']}")
            return {
                "ok": True,
                "dryRun": True,
                "changed": 0,
                "wouldChange": len(reachable),
                "rows": state["rows"],
                "message": f"试运行：{len(reachable)} 处能改，{len(unreachable)} 处找不到键",
                "backup": "",
                "failed": unreachable,
                "reloads": [],
            }

        files = []
        if core_rows:
            core_path = getattr(cfg, "config_path", "")
            if core_path:
                files.append(core_path)
        for scope in plugin_rows:
            entry = by_scope.get(scope)
            if entry and entry[4]:
                files.append(entry[4])
        backup_dir = backup_files(files)

        # 写 AstrBot 自己的配置
        failed: list[str] = []
        for row in core_rows:
            if not set_path(cfg, row["path"], target):
                failed.append(f"AstrBot · {row['path']}")
        if core_rows:
            try:
                cfg.save_config()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[模型同步] 写 AstrBot 配置失败")
                return {"ok": False, "message": f"写 AstrBot 配置失败：{exc}"}

        # 写插件配置
        reloads: list[dict] = []
        for scope, rows in plugin_rows.items():
            entry = by_scope.get(scope)
            if not entry:
                failed.append(scope)
                continue
            _scope, _display, data, live, path = entry
            touched = False
            for row in rows:
                if live is not None:
                    touched = set_path(live, row["path"], target) or touched
                elif path:
                    touched = set_path(data, row["path"], target) or touched
            if not touched:
                failed.append(f"{scope}（配置里没有这个键，跳过）")
                continue
            try:
                if live is not None and hasattr(live, "save_config"):
                    live.save_config()
                elif path:
                    dump_json(path, data)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[模型同步] 写 %s 配置失败", scope)
                failed.append(f"{scope}（写盘失败：{exc}）")
                continue

            if with_reload:
                reg_name = self._registered_name(scope)
                manager = getattr(self.context, "_star_manager", None)
                if reg_name is None:
                    reloads.append({"name": scope, "ok": False, "message": "插件没在运行，重启后生效"})
                elif manager is None:
                    reloads.append({"name": scope, "ok": False, "message": "拿不到插件管理器"})
                else:
                    try:
                        ok, error = await manager.reload(reg_name)
                        reloads.append({"name": scope, "ok": bool(ok), "message": error or ""})
                    except Exception as exc:  # noqa: BLE001
                        reloads.append({"name": scope, "ok": False, "message": str(exc)})

        message = f"改了 {len(wanted) - len(failed)} 处"
        if failed:
            message += f"，{len(failed)} 处没改成"
        logger.info("[模型同步] 目标=%s 模式=%s 备份=%s 失败=%s", target, mode, backup_dir, failed)
        return {
            "ok": True,
            "changed": len(wanted) - len(failed),
            "rows": self.build_state(target, mode)["rows"],
            "message": message,
            "backup": backup_dir,
            "failed": failed,
            "reloads": reloads,
        }

    async def _restore(self) -> dict:
        stamp = last_backup()
        if not stamp:
            return {"ok": False, "message": "还没有备份可以还原"}
        directory = os.path.join(backup_root(), stamp)
        manifest_path = os.path.join(directory, "manifest.json")
        if not os.path.exists(manifest_path):
            return {"ok": False, "message": "备份清单丢了，没法自动还原"}
        manifest = load_json(manifest_path)

        cfg = self._core_cfg()
        core_path = os.path.realpath(getattr(cfg, "config_path", "") or "")
        by_path = {}
        for _scope, _display, _data, live, path in self._plugin_targets():
            if path:
                by_path[os.path.realpath(path)] = live

        restored: list[str] = []
        mem_refreshed: list[str] = []
        for name, source in manifest.items():
            stored = os.path.join(directory, name)
            if not os.path.exists(stored):
                continue
            shutil.copy2(stored, source)
            restored.append(source)
            real = os.path.realpath(source)
            try:
                fresh = load_json(source)
            except Exception:  # noqa: BLE001
                continue
            # 文件回去了，内存里的也得跟着回去，否则要等重启才一致
            if core_path and real == core_path:
                _replace_in_place(cfg, fresh)
                mem_refreshed.append("AstrBot")
            else:
                live = by_path.get(real)
                if live is not None:
                    _replace_in_place(live, fresh)
                    mem_refreshed.append(os.path.basename(source))

        return {
            "ok": True,
            "message": f"还原了 {len(restored)} 个文件" + (
                f"，{len(mem_refreshed)} 处内存里的值也刷新了" if mem_refreshed else ""
            ),
            "files": restored,
            "refreshed": mem_refreshed,
            "backup": directory,
        }

    # ------------------------------------------------------- 接口 ----

    async def api_scan(self):
        target = request.query.get("target", "") or ""
        mode = request.query.get("mode", "all") or "all"
        try:
            state = self.build_state(target, mode)
            logger.debug(
                "[模型同步] 面板扫描 target=%s 待改=%s 用户=%s",
                state["target"], state["pending"], getattr(request, "username", ""),
            )
            return {"status": "ok", "data": state}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[模型同步] 扫描失败")
            return {"status": "error", "message": f"扫描失败：{exc}", "data": {}}

    async def api_apply(self):
        payload = await request.json(default={}) or {}
        target = str(payload.get("target") or "").strip()
        mode = str(payload.get("mode") or "all").strip()
        with_reload = bool(payload.get("reload", True))
        dry_run = bool(payload.get("dryRun", False))
        if mode not in ("all", "fix"):
            mode = "all"
        try:
            result = await self._apply(target, mode, with_reload, dry_run)
            if not result.get("ok"):
                return {"status": "error", "message": result.get("message", "同步失败"), "data": result}
            return {"status": "ok", "data": result}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[模型同步] 同步失败")
            return {"status": "error", "message": f"同步失败：{exc}", "data": {}}

    async def api_restore(self):
        try:
            result = await self._restore()
            if not result.get("ok"):
                return {"status": "error", "message": result.get("message", ""), "data": result}
            return {"status": "ok", "data": result}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[模型同步] 还原失败")
            return {"status": "error", "message": f"还原失败：{exc}", "data": {}}

    # ------------------------------------------------------- 指令 ----

    def _text_report(self, state: dict) -> str:
        lines = [f"目标模型：{state['target'] or '（没找到能用的对话模型）'}"]
        pending = [row for row in state["rows"] if row["action"] == SET]
        if not pending:
            lines.append("没有还钉在旧模型上的地方，不用改。")
        else:
            lines.append(f"要改 {len(pending)} 处：")
            for row in pending[:18]:
                lines.append(f"· {row['label']} → {row['reason']}")
            if len(pending) > 18:
                lines.append(f"· 还有 {len(pending) - 18} 处…")
        skipped = [row for row in state["rows"] if row["action"] == SKIP]
        if skipped:
            lines.append(f"另有 {len(skipped)} 处不动（向量/语音、空值、或目标没图片能力）。")
        lines.append("发「/模型同步 执行」就真的改；改之前会自动备份。")
        return "\n".join(lines)

    @filter.command("模型同步")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_model_sync(self, event: AstrMessageEvent, action: str = ""):
        action = (action or "").strip()
        if action in ("还原", "undo", "restore"):
            result = await self._restore()
            yield event.plain_result(result.get("message") or "还原结束")
            return
        state = self.build_state()
        if action in ("执行", "apply", "run"):
            if not state["target"]:
                yield event.plain_result("没找到能用的对话模型，先去面板把要用的那个打开。")
                return
            result = await self._apply(state["target"], "all", True)
            if not result.get("ok"):
                yield event.plain_result(result.get("message") or "同步失败")
                return
            lines = [result["message"], f"备份：{os.path.basename(result.get('backup') or '')}"]
            for item in result.get("reloads") or []:
                lines.append(f"重载 {item['name']}：{'好' if item['ok'] else '失败 ' + (item['message'] or '')}")
            for name in result.get("failed") or []:
                lines.append(f"没改成：{name}")
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result(self._text_report(state))
