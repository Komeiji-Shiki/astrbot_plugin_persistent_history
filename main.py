import os
import sqlite3
import time
import uuid
import aiohttp
import re
import shutil
from typing import List, Optional
import base64
import mimetypes

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain, Image, BaseMessageComponent

@register(
    "PersistentChat", 
    "YourName", 
    "一个可以持久化保存聊天记录（包括图片），支持多模态LLM上下文，并提供管理命令的插件", 
    "2.6.0" # 版本升级：修复消息重复注入问题，并精确实现对当前消息的增强逻辑
)
class PersistentChatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db_conn = None
        self.db_cursor = None
        self.plugin_data_dir = os.path.join("data", "persistent_chat")
        self.images_dir = os.path.join(self.plugin_data_dir, "images")
        self._setup_database_and_dirs()

    def _setup_database_and_dirs(self):
        try:
            os.makedirs(self.plugin_data_dir, exist_ok=True)
            os.makedirs(self.images_dir, exist_ok=True)
            db_path = os.path.join(self.plugin_data_dir, "chat_history.db")
            self.db_conn = sqlite3.connect(db_path, check_same_thread=False)
            self.db_cursor = self.db_conn.cursor()
            self.db_cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT,
                    message_text TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            ''')
            self.db_conn.commit()
            logger.info("PersistentChat: 数据库和目录初始化成功。")
        except Exception as e:
            logger.error(f"PersistentChat: 数据库或目录初始化失败: {e}")

    async def _download_image(self, url: str) -> Optional[str]:
        if not url or not url.startswith(('http://', 'https://')): return None
        try:
            file_ext = os.path.splitext(url.split('?')[0])[-1] or '.png'
            if len(file_ext) > 5 or len(file_ext) < 2: file_ext = '.png'
            filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{file_ext}"
            filepath = os.path.join(self.images_dir, filename)
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        with open(filepath, 'wb') as f: f.write(await resp.read())
                        return filename
            return None
        except Exception as e:
            logger.error(f"PersistentChat: 下载图片时发生错误: {e}")
            return None

    def _path_to_base64(self, file_path: str) -> Optional[str]:
        try:
            if not os.path.exists(file_path):
                logger.warning(f"PersistentChat: 文件不存在，无法转换为Base64: {file_path}")
                return None
            mime_type, _ = mimetypes.guess_type(file_path)
            if not mime_type: mime_type = "image/png"
            with open(file_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            return f"data:{mime_type};base64,{encoded_string}"
        except Exception as e:
            logger.error(f"PersistentChat: 转换文件到Base64失败: {file_path}, 错误: {e}")
            return None

    async def _process_message_chain(self, chain: List[BaseMessageComponent]) -> str:
        parts = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text.strip():
                parts.append(comp.text.strip())
            elif isinstance(comp, Image):
                url_to_download = getattr(comp, 'url', None) or getattr(comp, 'file', None)
                if url_to_download:
                    saved_filename = await self._download_image(url_to_download)
                    parts.append(f"[图片:{saved_filename}]" if saved_filename else "[图片下载失败]")
        return " ".join(parts).strip()

    def _save_log_to_db(self, session_id, sender_id, sender_name, message_text):
        if not message_text: return
        try:
            self.db_cursor.execute(
                "INSERT INTO chat_logs (session_id, sender_id, sender_name, message_text, timestamp) VALUES (?, ?, ?, ?, ?)",
                (session_id, sender_id, sender_name, message_text, int(time.time()))
            )
            self.db_conn.commit()
        except Exception as e:
            logger.error(f"PersistentChat: 记录到数据库时出错: {e}")
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def log_user_message(self, event: AstrMessageEvent):
        if event.message_str.strip().startswith('/'): return
        if (event.is_private_chat() and not self.config.get('log_private_messages', False)) or \
           (not event.is_private_chat() and not self.config.get('log_group_messages', True)) or \
           (event.get_sender_id() == event.get_self_id()): return
        message_chain = event.get_messages()
        processed_text = await self._process_message_chain(message_chain)
        self._save_log_to_db(event.unified_msg_origin, event.get_sender_id(), event.get_sender_name(), processed_text)

    @filter.after_message_sent()
    async def log_bot_response(self, event: AstrMessageEvent):
        if event.get_extra("is_command_response"): return
        if not self.config.get('log_self_messages', True): return
        result = event.get_result()
        if not result or not result.chain: return
        processed_text = await self._process_message_chain(result.chain)
        self._save_log_to_db(event.unified_msg_origin, event.get_self_id(), "assistant", processed_text)

    @filter.on_llm_request(priority=1)
    async def inject_chat_history(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get('inject_context', True): return
        max_history = self.config.get('max_history_messages', 20)
        if max_history <= 0: return

        try:
            image_pattern = re.compile(r"\[图片:([^\]]+)\]")
            bot_self_id = event.get_self_id()
            current_turn_contexts = req.contexts or []
            req.contexts.clear()

            # --- 1. 获取所有相关记录，包括当前消息 ---
            self.db_cursor.execute(
                "SELECT sender_id, sender_name, message_text FROM chat_logs WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (event.unified_msg_origin, max_history + 1)
            )
            rows = self.db_cursor.fetchall()
            if not rows: # 如果数据库为空，直接使用框架的上下文
                req.contexts = current_turn_contexts
                return

            # --- 2. 分离当前消息和历史记录 ---
            current_msg_row = rows.pop(0) # 最新的消息是当前消息
            history_rows = rows
            history_rows.reverse() # 恢复时间顺序

            # --- 3. 处理历史记录 ---
            history_contexts = []
            text_only_contexts = []
            for sender_id, sender_name, message_text in history_rows:
                role = "assistant" if sender_id == bot_self_id else "user"
                image_filenames = image_pattern.findall(message_text)
                text_part = image_pattern.sub("", message_text).strip()
                if image_filenames and not text_part: text_part = "[用户发送了图片]"

                content_list = []
                for filename in image_filenames:
                    image_path = os.path.join(self.images_dir, filename)
                    base64_uri = self._path_to_base64(image_path)
                    if base64_uri: content_list.append({"type": "image_url", "image_url": {"url": base64_uri}})
                if text_part: content_list.append({"type": "text", "text": text_part})
                if not content_list: continue

                text_only_content = f"{'[图片]' * len(image_filenames)} {text_part}".strip()
                if role == "user":
                    display_name = sender_name or "User"
                    text_only_content = f"{display_name}: {text_only_content}"
                    if content_list and content_list[0]['type'] == 'text': content_list[0]['text'] = f"{display_name}: {content_list[0]['text']}"
                    else: content_list.insert(0, {'type': 'text', 'text': f"{display_name}: "})
                
                history_contexts.append({'role': role, 'content': content_list})
                text_only_contexts.append({'role': role, 'content': text_only_content})

            # --- 4. 根据规则增强当前消息 ---
            _sender_id, _sender_name, current_message_text = current_msg_row
            image_filenames = image_pattern.findall(current_message_text)
            text_part = image_pattern.sub("", current_message_text).strip()
            
            # 找到框架提供的当前用户消息
            target_index = -1
            for i in range(len(current_turn_contexts) - 1, -1, -1):
                if current_turn_contexts[i].get('role') == 'user':
                    target_index = i
                    break

            if target_index != -1:
                if image_filenames and not text_part: # 规则3: 只有图片
                    # 替换掉框架的文本
                    current_turn_contexts[target_index]['content'] = [
                        {"type": "text", "text": "[用户最新消息只发送了图片]"}
                    ]
                
                if image_filenames: # 规则2 & 3: 只要有图片，就注入
                    image_parts = []
                    for filename in image_filenames:
                        image_path = os.path.join(self.images_dir, filename)
                        base64_uri = self._path_to_base64(image_path)
                        if base64_uri: image_parts.append({"type": "image_url", "image_url": {"url": base64_uri}})
                    
                    # 确保 content 是 list
                    content = current_turn_contexts[target_index]['content']
                    if not isinstance(content, list):
                        content = [{"type": "text", "text": str(content)}]
                    
                    # 将图片插入到内容最前面
                    current_turn_contexts[target_index]['content'] = image_parts + content

            # --- 5. 合并历史和（增强后的）当前消息 ---
            final_contexts = history_contexts + current_turn_contexts
            if not final_contexts:
                event.set_extra('text_only_history', text_only_contexts)
                return

            merged_contexts = [final_contexts[0]]
            for i in range(1, len(final_contexts)):
                current_msg, last_msg = final_contexts[i], merged_contexts[-1]
                if current_msg['role'] == last_msg['role']:
                    last_content = last_msg['content'] if isinstance(last_msg['content'], list) else [{'type': 'text', 'text': str(last_msg['content'])}]
                    current_content = current_msg['content'] if isinstance(current_msg['content'], list) else [{'type': 'text', 'text': str(current_msg['content'])}]
                    last_msg['content'] = last_content + current_content
                else:
                    merged_contexts.append(current_msg)

            for msg in merged_contexts:
                content = msg.get('content')
                if isinstance(content, list) and all(item.get('type') == 'text' for item in content):
                    msg['content'] = " ".join(item.get('text', '') for item in content).strip()

            req.contexts = merged_contexts
            event.set_extra('text_only_history', text_only_contexts)
            logger.debug(f"PersistentChat: 已注入 {len(history_contexts)} 条历史并增强当前消息。最终上下文: {len(req.contexts)} 条。")

        except Exception as e:
            logger.error(f"PersistentChat: 注入历史记录时出错: {e}", exc_info=True)


    # --- 管理命令部分 (保持不变) ---
    @filter.command_group("chathistory", alias={"聊天历史", "pchat"})
    def history_cmd_group(self):
        """管理持久化聊天记录"""
        pass

    @history_cmd_group.command("view", alias={"查看"})
    async def view_history(self, event: AstrMessageEvent, count: int = 5):
        """查看最近的聊天记录。用法: /chathistory view [条数]"""
        event.set_extra("is_command_response", True)
        if count <= 0 or count > 50:
            yield event.plain_result("查看的条数必须在 1 到 50 之间。")
            return
        try:
            self.db_cursor.execute(
                "SELECT sender_name, message_text FROM chat_logs WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (event.unified_msg_origin, count)
            )
            rows = self.db_cursor.fetchall()
            rows.reverse()
            if not rows:
                yield event.plain_result("当前会话没有聊天记录。")
                return
            response_text = f"最近 {len(rows)} 条聊天记录:\n" + "-"*20
            for sender_name, message_text in rows:
                response_text += f"\n[{sender_name or '未知'}]: {message_text}"
            image_url = await self.text_to_image(response_text)
            yield event.image_result(image_url)
        except Exception as e:
            logger.error(f"PersistentChat: 查看历史记录时出错: {e}")
            yield event.plain_result(f"查看历史记录失败: {e}")

    @history_cmd_group.command("clear", alias={"清空"})
    async def clear_history(self, event: AstrMessageEvent):
        """清空当前会话的所有聊天记录及关联图片。"""
        event.set_extra("is_command_response", True)
        try:
            self.db_cursor.execute("SELECT message_text FROM chat_logs WHERE session_id = ?", (event.unified_msg_origin,))
            rows = self.db_cursor.fetchall()
            image_pattern = re.compile(r"\[图片:([^\]]+)\]")
            images_to_delete = set()
            for row in rows:
                found_images = image_pattern.findall(row[0])
                for img in found_images:
                    images_to_delete.add(img)
            self.db_cursor.execute("DELETE FROM chat_logs WHERE session_id = ?", (event.unified_msg_origin,))
            self.db_conn.commit()
            deleted_count = self.db_cursor.rowcount
            deleted_images_count = 0
            for filename in images_to_delete:
                try:
                    filepath = os.path.join(self.images_dir, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                        deleted_images_count += 1
                except Exception as img_e:
                    logger.warning(f"PersistentChat: 删除图片 {filename} 时失败: {img_e}")
            yield event.plain_result(f"成功清空了当前会话的 {deleted_count} 条聊天记录，并删除了 {deleted_images_count} 张关联图片。")
        except Exception as e:
            logger.error(f"PersistentChat: 清空当前会话历史时出错: {e}")
            yield event.plain_result(f"清空失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @history_cmd_group.command("clear_all", alias={"清空全部"})
    async def clear_all_history(self, event: AstrMessageEvent):
        """(仅管理员) 清空所有聊天记录和已保存的图片。"""
        event.set_extra("is_command_response", True)
        try:
            self.db_cursor.execute("SELECT COUNT(*) FROM chat_logs")
            deleted_count = self.db_cursor.fetchone()[0]
            self.db_cursor.execute("DELETE FROM chat_logs")
            self.db_conn.commit()
            if os.path.exists(self.images_dir):
                shutil.rmtree(self.images_dir)
            os.makedirs(self.images_dir, exist_ok=True)
            yield event.plain_result(f"操作成功！已清空数据库中 {deleted_count} 条记录，并删除了所有缓存图片。")
        except Exception as e:
            logger.error(f"PersistentChat: 清空所有历史时出错: {e}")
            yield event.plain_result(f"清空全部失败: {e}")
            
    async def terminate(self):
        """插件终止时，安全地关闭数据库连接。"""
        if self.db_conn:
            self.db_conn.close()
            logger.info("PersistentChat: 数据库连接已关闭。")

