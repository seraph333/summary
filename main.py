# encoding:utf-8

import json
import os
import re
import time
import sqlite3
import requests
from urllib.parse import urlparse
import time

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
from common import const
from plugins import *

@plugins.register(
    name="Summary",
    desire_priority=10,
    hidden=False,
    enabled=True,
    desc="聊天记录总结助手",
    version="1.0",
    author="lanvent",
)
class Summary(Plugin):
    # Default configuration values
    open_ai_api_base = "https://api.openai.com/v1"
    open_ai_model = "gpt-4o-mini"
    max_tokens = 1500
    max_input_tokens = 4000  # 默认限制输入4000个token
    prompt = '''
    你是一个聊天记录总结的AI助手。
    1. 做群聊总结和摘要，主次层次分明；
    2. 尽量突出重要内容以及关键信息（重要的关键字/数据/观点/结论等），请表达呈现出来，避免过于简略而丢失信息量；
    3. 允许有多个主题/话题，分开描述；
    4. 弱化非关键发言人的对话内容。
    5. 如果把多个小话题合并成1个话题能更完整的体现对话内容，可以考虑合并，否则不合并；
格式：
1️⃣{Topic}{热度(用1-5个🔥表示)}
• 时间：{时:分} - {时:分}(不显示年月日)
• 参与者：
• 内容：
• 结论：
………

聊天记录格式：
[x]是emoji表情或者是对图片和声音文件的说明，消息最后出现<T>表示消息触发了群聊机器人的回复，内容通常是提问，若带有特殊符号如#和$则是触发你无法感知的某个插件功能，聊天记录中不包含你对这类消息的回复，可降低这些消息的权重。请不要在回复中包含聊天记录格式中出现的符号。'''

    def __init__(self):
        super().__init__()
        try:
            self.config = self._load_config()
            # Load configuration with defaults
            self.open_ai_api_base = self.config.get("open_ai_api_base", self.open_ai_api_base)
            self.open_ai_api_key = self.config.get("open_ai_api_key", "")
            
            # Validate API key
            if not self.open_ai_api_key:
                logger.error("[Summary] API key not found in config")
                raise Exception("API key not configured")
                
            self.open_ai_model = self.config.get("open_ai_model", self.open_ai_model)
            self.max_tokens = self.config.get("max_tokens", self.max_tokens)
            self.max_input_tokens = self.config.get("max_input_tokens", self.max_input_tokens)  # 默认限制输入4000个token
            self.prompt = self.config.get("prompt", self.prompt)

            # Initialize database
            curdir = os.path.dirname(__file__)
            db_path = os.path.join(curdir, "chat.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_database()

            # Register handlers
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
            logger.info("[Summary] initialized with config: %s", self.config)
        except Exception as e:
            logger.error(f"[Summary] initialization failed: {e}")
            raise e

    def _init_database(self):
        """Initialize the database schema"""
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER, is_triggered INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        
        # Check if is_triggered column exists
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = False
        for column in c.fetchall():
            if column[1] == 'is_triggered':
                column_exists = True
                break
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")
        self.conn.commit()

    def _load_config(self):
        """Load configuration from config.json"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            if not os.path.exists(config_path):
                return {}
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Summary] load config failed: {e}")
            return {}

    def _get_openai_chat_url(self):
        """Get the OpenAI chat completions API URL"""
        return f"{self.open_ai_api_base}/chat/completions"

    def _get_openai_headers(self):
        """Get the headers for OpenAI API requests"""
        return {
            'Authorization': f"Bearer {self.open_ai_api_key}",
            'Host': urlparse(self.open_ai_api_base).netloc,
            'Content-Type': 'application/json'
        }

    def _get_openai_payload(self, content):
        """Prepare the payload for OpenAI API request"""
        messages = [{"role": "user", "content": content}]
        return {
            'model': self.open_ai_model,
            'messages': messages,
            'max_tokens': self.max_tokens
        }

    def _chat_completion(self, content):
        """Make a request to OpenAI chat completions API"""
        try:
            url = self._get_openai_chat_url()
            headers = self._get_openai_headers()
            payload = self._get_openai_payload(content)
            
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if response.status_code == 401:
                logger.error("[Summary] API key is invalid or expired")
                raise Exception("Invalid API key")
            elif response.status_code == 429:
                logger.error("[Summary] Rate limit exceeded")
                raise Exception("Rate limit exceeded")
            elif response.status_code != 200:
                logger.error(f"[Summary] API request failed with status {response.status_code}: {response.text}")
                raise Exception(f"API request failed: {response.text}")
                
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except requests.exceptions.RequestException as e:
            logger.error(f"[Summary] Network error during API request: {e}")
            raise Exception(f"Network error: {str(e)}")
        except Exception as e:
            logger.error(f"[Summary] OpenAI API request failed: {e}")
            raise e

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        """Insert a record into the database"""
        c = self.conn.cursor()
        logger.debug("[Summary] insert record: {} {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        self.conn.commit()
    
    def _get_records(self, session_id, start_timestamp=0, limit=9999):
        """Get records from the database"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? ORDER BY timestamp DESC LIMIT ?", (session_id, start_timestamp, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        """Handle received messages"""
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        username = None
        session_id = cmsg.from_user_id
        if self.config.get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False):
            match_prefix = check_prefix(content, self.config.get('group_chat_prefix'))
            match_contain = check_contain(content, self.config.get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not self.config.get("group_at_off", False):
                is_triggered = True
        else:
            match_prefix = check_prefix(content, self.config.get('single_chat_prefix',['']))
            if match_prefix is not None:
                is_triggered = True

        self._insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time, int(is_triggered))
        logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def _check_tokens(self, records, max_tokens=3600):
        """Prepare chat content for summarization"""
        messages = []
        total_length = 0
        max_input_chars = self.max_input_tokens * 4  # 粗略估计：1个token约等于4个字符
        
        # 记录已经是倒序的（最新的在前），直接处理
        for record in records:
            username = record[2] or ""  # Handle None username
            content = record[3] or ""   # Handle None content
            timestamp = record[5]
            is_triggered = record[6]
            
            # Convert timestamp to readable format
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
            
            if record[4] in [str(ContextType.IMAGE),str(ContextType.VOICE)]:
                content = f"[{record[4]}]"
            
            sentence = f'[{time_str}] {username}: "{content}"'
            if is_triggered:
                sentence += " <T>"
                
            # 检查添加这条记录后是否会超出限制
            if total_length + len(sentence) + 2 > max_input_chars:  # 2是换行符的长度
                logger.info(f"[Summary] Input length limit reached at {total_length} chars")
                break
                
            messages.append(sentence)
            total_length += len(sentence) + 2

        # 将消息按时间顺序拼接（从早到晚）
        query = "\n\n".join(messages[::-1])
        return f"{self.prompt}\n\n需要你总结的聊天记录如下：{query}"

    def _split_messages_to_summarys(self, records, max_tokens_persession=3600, max_summarys=8):
        """Split messages into chunks and summarize each chunk"""
        summarys = []
        count = 0

        while len(records) > 0 and len(summarys) < max_summarys:
            content = self._check_tokens(records, max_tokens_persession)
            if not content:
                break

            try:
                result = self._chat_completion(content)
                summarys.append(result)
                count += 1
            except Exception as e:
                logger.error(f"[Summary] summarization failed: {e}")
                break

            if len(records) > max_tokens_persession:
                records = records[max_tokens_persession:]
            else:
                break

        return summarys

    def on_handle_context(self, e_context: EventContext):
        """Handle context for summarization"""
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            limit = 99
            start_time = 0
            
            if len(clist) > 1:
                try:
                    # 第一个参数作为时间偏移（秒）
                    start_time = int(time.time()) + int(clist[1])
                except:
                    pass
                
            if len(clist) > 2:
                try:
                    # 第二个参数作为消息数量限制
                    limit = int(clist[2])
                except:
                    pass

            msg:ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if self.config.get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname
            records = self._get_records(session_id, start_time, limit)
            
            if not records:
                reply = Reply(ReplyType.ERROR, "没有找到聊天记录")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            summarys = self._split_messages_to_summarys(records)
            if not summarys:
                reply = Reply(ReplyType.ERROR, "总结失败")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            result = "\n\n".join(summarys)
            reply = Reply(ReplyType.TEXT, result)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text
