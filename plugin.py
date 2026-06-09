"""群友语录 — 群聊图片投稿与随机语录插件"""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import time
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "quotes.db")
IMAGES_DIR = os.path.join(DATA_DIR, "images")


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用群友语录插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class LimitsConfig(PluginConfigBase):
    """限制配置。"""

    __ui_label__ = "限制"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    max_quotes_per_group: int = Field(
        default=500, description="每个群最大语录数量，超出后自动清理最旧记录"
    )


class GroupQuotesConfig(PluginConfigBase):
    """群友语录插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------


class GroupQuotesPlugin(MaiBotPlugin):
    """群友语录插件：群内图片投稿与随机语录。"""

    config_model = GroupQuotesConfig

    def __init__(self) -> None:
        super().__init__()
        self._db: sqlite3.Connection | None = None

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        """插件加载时初始化数据目录和数据库。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(IMAGES_DIR, exist_ok=True)

        self._db = sqlite3.connect(DB_PATH)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_nickname TEXT NOT NULL DEFAULT '',
                image_hash TEXT NOT NULL,
                image_path TEXT NOT NULL,
                message_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_quotes_group_id
            ON group_quotes(group_id)
            """
        )
        self._db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_group_quotes_dedup
            ON group_quotes(group_id, image_hash)
            """
        )
        self._db.commit()

        self.ctx.logger.info("群友语录插件已加载")

    async def on_unload(self) -> None:
        """插件卸载时关闭数据库连接。"""
        if self._db:
            self._db.close()
            self._db = None
        self.ctx.logger.info("群友语录插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热重载时执行。"""

    # ---- /投稿 ----

    @Command("submit_quote", description="投稿群友语录（回复图片使用）", pattern=r"/投稿")
    async def handle_submit_quote(
        self,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: dict | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投稿 命令：回复一条含图片的消息，将该图片投稿到本群语录库。"""
        del kwargs

        # 1. 仅支持群聊
        if not group_id:
            await self.ctx.send.text("群友语录仅支持群聊使用", stream_id)
            return False, "非群聊消息", True

        # 2. 从当前消息中找 reply 组件
        if not message or not isinstance(message, dict):
            await self.ctx.send.text("无法获取消息内容", stream_id)
            return False, "消息为空", True

        raw_message = message.get("raw_message", [])
        reply_component = _find_component(raw_message, "reply")

        if not reply_component:
            await self.ctx.send.text("请回复一张图片来投稿 /投稿", stream_id)
            return False, "无回复", True

        # 3. 提取被回复消息 ID
        reply_data = reply_component.get("data", {})
        if isinstance(reply_data, dict):
            target_message_id = str(reply_data.get("target_message_id", "")).strip()
        else:
            target_message_id = str(reply_data).strip()

        if not target_message_id:
            await self.ctx.send.text("无法获取被回复的消息", stream_id)
            return False, "无目标消息 ID", True

        # 4. 通过 SDK 获取被回复的消息（含图片二进制）
        # ctx.message.get_by_id 成功时直接返回消息字典（SDK 已解包 success/message），
        # 失败时返回 None 或包含 error 的字典。
        result = await self.ctx.message.get_by_id(
            target_message_id, include_binary_data=True
        )
        if not result or not isinstance(result, dict) or "error" in result:
            error_msg = result.get("error", "未知错误") if isinstance(result, dict) else "返回为空"
            self.ctx.logger.warning(f"获取被回复消息失败: {error_msg}")
            await self.ctx.send.text("被回复的消息不存在或已过期", stream_id)
            return False, "获取消息失败", True

        # result 本身就是消息字典，无需再取 .message
        replied_message = result

        # 5. 从被回复消息中找图片组件
        replied_raw = replied_message.get("raw_message", [])
        image_component = _find_component(replied_raw, "image")

        if not image_component:
            await self.ctx.send.text("被回复的消息中没有图片", stream_id)
            return False, "无图片", True

        # 6. 提取图片数据
        image_hash = str(image_component.get("hash", "")).strip()
        image_base64 = str(image_component.get("binary_data_base64", "")).strip()

        if not image_base64:
            await self.ctx.send.text(
                "无法获取图片数据（消息可能已过期）", stream_id
            )
            return False, "无图片数据", True

        # 7. 计算哈希（如果缺失）
        if not image_hash:
            image_bytes = base64.b64decode(image_base64)
            image_hash = hashlib.sha256(image_bytes).hexdigest()

        # 8. 保存图片文件
        group_image_dir = os.path.join(IMAGES_DIR, group_id)
        os.makedirs(group_image_dir, exist_ok=True)
        image_filename = f"{image_hash}.png"
        image_path = os.path.join(group_image_dir, image_filename)

        if not os.path.exists(image_path):
            image_bytes = base64.b64decode(image_base64)
            with open(image_path, "wb") as f:
                f.write(image_bytes)

        # 9. 获取投稿者昵称
        user_nickname = _extract_user_nickname(message)

        # 10. 插入数据库
        assert self._db is not None
        try:
            self._db.execute(
                """
                INSERT INTO group_quotes
                    (group_id, user_id, user_nickname, image_hash,
                     image_path, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    user_nickname,
                    image_hash,
                    image_path,
                    target_message_id,
                    time.time(),
                ),
            )
            self._db.commit()
        except sqlite3.IntegrityError:
            await self.ctx.send.text("这张图片已经投稿过了", stream_id)
            return False, "重复投稿", True

        # 11. 超出上限时清理最旧记录
        max_quotes = self.config.limits.max_quotes_per_group
        cursor = self._db.execute(
            "SELECT COUNT(*) FROM group_quotes WHERE group_id = ?",
            (group_id,),
        )
        count = cursor.fetchone()[0]
        if count > max_quotes:
            self._db.execute(
                """
                DELETE FROM group_quotes WHERE group_id = ? AND id IN (
                    SELECT id FROM group_quotes
                    WHERE group_id = ?
                    ORDER BY created_at ASC LIMIT ?
                )
                """,
                (group_id, group_id, count - max_quotes),
            )
            self._db.commit()

        await self.ctx.send.text("投稿成功！", stream_id)
        return True, "投稿成功", True

    # ---- /群友语录 ----

    @Command(
        "random_quote",
        description="随机获取一条群友语录",
        pattern=r"/群友语录",
    )
    async def handle_random_quote(
        self,
        stream_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /群友语录 命令：随机发送一条本群投稿的图片。"""
        del kwargs

        # 1. 仅支持群聊
        if not group_id:
            await self.ctx.send.text("群友语录仅支持群聊使用", stream_id)
            return False, "非群聊消息", True

        # 2. 查询随机语录
        if not self._db:
            await self.ctx.send.text("语录库未初始化", stream_id)
            return False, "数据库未初始化", True

        cursor = self._db.execute(
            """
            SELECT image_path, image_hash
            FROM group_quotes
            WHERE group_id = ?
            ORDER BY RANDOM() LIMIT 1
            """,
            (group_id,),
        )
        row = cursor.fetchone()

        if not row:
            await self.ctx.send.text(
                "本群还没有语录，快来 /投稿 吧！", stream_id
            )
            return False, "语录为空", True

        image_path, image_hash = row

        # 3. 读取并发送图片
        if not os.path.isfile(image_path):
            # 文件丢失，清理孤立记录
            self._db.execute(
                "DELETE FROM group_quotes WHERE group_id = ? AND image_hash = ?",
                (group_id, image_hash),
            )
            self._db.commit()
            await self.ctx.send.text("语录图片文件丢失，请重新投稿", stream_id)
            return False, "文件丢失", True

        with open(image_path, "rb") as f:
            image_bytes = f.read()
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        await self.ctx.send.image(image_base64, stream_id)
        return True, "已发送语录", True


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _find_component(
    raw_message: list[Any], comp_type: str
) -> dict[str, Any] | None:
    """在 raw_message 列表中查找指定类型的组件。"""
    for comp in raw_message:
        if isinstance(comp, dict) and comp.get("type") == comp_type:
            return comp
    return None


def _extract_user_nickname(message: dict[str, Any]) -> str:
    """从消息字典中提取发送者昵称。"""
    message_info = message.get("message_info", {})
    if isinstance(message_info, dict):
        user_info = message_info.get("user_info", {})
        if isinstance(user_info, dict):
            return str(user_info.get("user_nickname", ""))
    return ""


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_plugin() -> GroupQuotesPlugin:
    """创建群友语录插件实例。"""
    return GroupQuotesPlugin()
