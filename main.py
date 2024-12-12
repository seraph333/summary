# encoding:utf-8

import json
import os
import time
import sqlite3
import requests
from urllib.parse import urlparse

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
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
    # 默认配置值
    open_ai_api_base = "https://api.openai.com/v1"
    open_ai_model = "gpt-4o-mini"
    max_tokens = 2000
    max_input_tokens = 8000  # 默认限制输入 8000 个 token
    prompt = '''
    你是一个聊天记录总结的AI助手，以下是默认规则和格式，如果有用户特定指令，以用户指令为准：
    1. 做群聊总结和摘要，主次层次分明；
    2. 尽量突出重要内容以及关键信息（重要的关键字/数据/观点/结论等），请表达呈现出来，避免过于简略而丢失信息量；
    3. 允许有多个主题/话题，分开描述；
    4. 弱化非关键发言人的对话内容。
    5. 如果把多个小话题合并成1个话题能更完整的体现对话内容，可以考虑合并，否则不合并；
格式：
1️⃣[Topic][热度(用1-5个🔥表示)]
• 时间：月-日 时:分 - -日 时:分(不显示年)
• 参与者：
• 内容：
• 结论：
………

用户指令:{custom_prompt}

聊天记录格式：
[x]是emoji表情或者是对图片和声音文件的说明，消息最后出现<T>表示消息触发了群聊机器人的回复，内容通常是提问，若带有特殊符号如#和$则是触发你无法感知的某个插件功能，聊天记录中不包含你对这类消息的回复，可降低这些消息的权重。请不要在回复中包含聊天记录格式中出现的符号。'''

    def __init__(self):
        super().__init__()
        try:
            self.config = self._load_config()
            # 加载配置，使用默认值
            self.open_ai_api_base = self.config.get("open_ai_api_base", self.open_ai_api_base)
            self.open_ai_api_key = self.config.get("open_ai_api_key", "")
            
            # 验证 API 密钥
            if not self.open_ai_api_key:
                logger.error("[Summary] API 密钥未在配置中找到")
                raise Exception("API 密钥未配置")
                
            self.open_ai_model = self.config.get("open_ai_model", self.open_ai_model)
            self.max_tokens = self.config.get("max_tokens", self.max_tokens)
            self.max_input_tokens = self.config.get("max_input_tokens", self.max_input_tokens)
            self.prompt = self.config.get("prompt", self.prompt)

            # 初始化数据库
            curdir = os.path.dirname(__file__)
            db_path = os.path.join(curdir, "chat.db")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_database()

            # 注册事件处理器
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
            logger.info("[Summary] 初始化完成，配置: %s", self.config)
        except Exception as e:
            logger.error(f"[Summary] 初始化失败: {e}")
            raise e

    def _init_database(self):
        """初始化数据库架构"""
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER, is_triggered INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        
        # 检查 is_triggered 列是否存在
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
        """从 config.json 加载配置"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            if not os.path.exists(config_path):
                return {}
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Summary] 加载配置失败: {e}")
            return {}

    def _get_openai_chat_url(self):
        """获取 OpenAI 聊天补全 API URL"""
        return f"{self.open_ai_api_base}/chat/completions"

    def _get_openai_headers(self):
        """获取 OpenAI API 请求头"""
        return {
            'Authorization': f"Bearer {self.open_ai_api_key}",
            'Host': urlparse(self.open_ai_api_base).netloc,
            'Content-Type': 'application/json'
        }

    def _get_openai_payload(self, content):
        """准备 OpenAI API 请求载荷"""
        messages = [{"role": "user", "content": content}]
        return {
            'model': self.open_ai_model,
            'messages': messages,
            'max_tokens': self.max_tokens
        }

    def _chat_completion(self, content, custom_prompt=None):
        """
        调用 OpenAI 聊天补全 API
        
        :param content: 需要总结的聊天内容
        :param custom_prompt: 可选的自定义 prompt，用于替换默认 prompt
        :return: 总结后的文本
        """
        try:
            # 使用默认 prompt
            prompt_to_use = self.prompt
            
            # 如果提供了自定义 prompt，则替换占位符
            if custom_prompt is not None:
                # 如果 custom_prompt 为 "无"，则使用空字符串
                replacement_prompt = "" if custom_prompt == "无" else custom_prompt
                prompt_to_use = prompt_to_use.replace("{custom_prompt}", replacement_prompt)
            
            # 打印完整的提示词
            logger.info(f"[Summary] 完整提示词: {prompt_to_use}")
            
            # 准备完整的载荷
            payload = {
                "model": self.open_ai_model,
                "messages": [
                    {"role": "system", "content": prompt_to_use},
                    {"role": "user", "content": content}
                ],
                "max_tokens": self.max_tokens
            }
            
            # 获取 OpenAI API URL 和请求头
            url = self._get_openai_chat_url()
            headers = self._get_openai_headers()
            
            # 发送 API 请求
            response = requests.post(url, headers=headers, json=payload)
            
            # 检查并处理响应
            if response.status_code == 200:
                result = response.json()
                summary = result['choices'][0]['message']['content'].strip()
                return summary
            else:
                logger.error(f"[Summary] OpenAI API 错误: {response.text}")
                return f"总结失败：{response.text}"
        
        except Exception as e:
            logger.error(f"[Summary] 总结生成失败: {e}")
            return f"总结失败：{str(e)}"

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        """将记录插入到数据库"""
        c = self.conn.cursor()
        logger.debug("[Summary] 插入记录: {} {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        self.conn.commit()
    
    def _get_records(self, session_id, start_timestamp=0, limit=9999):
        """从数据库获取记录"""
        c = self.conn.cursor()
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? ORDER BY timestamp DESC LIMIT ?", (session_id, start_timestamp, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        """处理接收到的消息"""
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
        """准备用于总结的聊天内容"""
        messages = []
        total_length = 0
        max_input_chars = self.max_input_tokens * 4  # 粗略估计：1个 token 约等于 4 个字符
        
        # 记录已经是倒序的（最新的在前），直接处理
        for record in records:
            username = record[2] or ""  # 处理空用户名
            content = record[3] or ""   # 处理空内容
            timestamp = record[5]
            is_triggered = record[6]
            
            # 将时间戳转换为可读格式
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
            
            if record[4] in [str(ContextType.IMAGE),str(ContextType.VOICE)]:
                content = f"[{record[4]}]"
            
            sentence = f'[{time_str}] {username}: "{content}"'
            if is_triggered:
                sentence += " <T>"
                
            # 检查添加此记录后是否会超出限制
            if total_length + len(sentence) + 2 > max_input_chars:  # 2 是换行符的长度
                logger.info(f"[Summary] 输入长度限制已达到 {total_length} 个字符")
                break
                
            messages.append(sentence)
            total_length += len(sentence) + 2

        # 将消息按时间顺序拼接（从早到晚）
        query = "\n\n".join(messages[::-1])
        return query

    def _split_messages_to_summarys(self, records, custom_prompt="", max_tokens_persession=3600, max_summarys=8):
        """将消息分割成块并总结每个块"""
        summarys = []
        count = 0

        while len(records) > 0 and len(summarys) < max_summarys:
            query = self._check_tokens(records, max_tokens_persession)
            if not query:
                break

            try:
                content = f"{self.prompt.replace('{custom_prompt}', custom_prompt)}\n\n需要你总结的聊天记录如下：{query}"
                result = self._chat_completion(content, custom_prompt)
                summarys.append(result)
                count += 1
            except Exception as e:
                logger.error(f"[Summary] 总结失败: {e}")
                break

            if len(records) > max_tokens_persession:
                records = records[max_tokens_persession:]
            else:
                break

        return summarys

    def _parse_summary_command(self, command_parts):
        """
        解析总结命令，支持以下格式：
        $总结 100                   # 最近100条消息
        $总结 -7200 100             # 过去2小时内的消息，最多100条
        $总结 -86400                # 过去24小时内的消息
        $总结 100 自定义指令         # 最近100条消息，使用自定义指令
        $总结 -7200 100 自定义指令   # 过去2小时内的消息，最多100条，使用自定义指令
        """
        current_time = int(time.time())
        custom_prompt = ""  # 初始化为空字符串
        start_timestamp = 0
        limit = 9999  # 默认最大消息数

        # 处理时间戳和消息数量
        for part in command_parts:
            if part.startswith('-') and part[1:].isdigit():
                # 负数时间戳：表示从过去多少秒开始
                start_timestamp = current_time + int(part)
            elif part.isdigit():
                # 如果是正整数，判断是消息数量还是时间戳
                if int(part) > 1000:  # 假设大于1000的数字被视为时间戳
                    start_timestamp = int(part)
                else:
                    limit = int(part)
            else:
                # 非数字部分被视为自定义指令
                custom_prompt += part + " "

        custom_prompt = custom_prompt.strip()
        return start_timestamp, limit, custom_prompt

    def on_handle_context(self, e_context: EventContext):
        """处理上下文，进行总结"""
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = self.config.get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            
            # 解析命令
            start_time, limit, custom_prompt = self._parse_summary_command(clist[1:])


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
            
            summarys = self._split_messages_to_summarys(records, custom_prompt)
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
