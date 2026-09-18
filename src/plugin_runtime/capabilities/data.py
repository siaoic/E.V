from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

import re
import time

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage
    from src.chat.message_receive.chat_manager import BotChatSession

logger = get_logger("plugin_runtime.integration")


class RuntimeDataCapabilityMixin:
    @staticmethod
    def _normalize_optional_bool(value: Any) -> Optional[bool]:
        """将插件入参中的布尔值规范化，未提供时返回 None。"""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized_value = value.strip().lower()
            if normalized_value in {"1", "true", "yes", "on"}:
                return True
            if normalized_value in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    async def _cap_database_query(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import database_service 

        model_name: str = args.get("model_name", "")
        if not model_name:
            return {"success": False, "error": "缺少必要参数 model_name"}

        try:
            import src.common.database.database_model as db_models

            model_class = getattr(db_models, model_name, None)
            if model_class is None:
                return {"success": False, "error": f"未找到数据模型: {model_name}"}

            query_type = args.get("query_type", "get")
            if query_type == "get":
                result = await database_service.db_get(
                    model_class=model_class,
                    filters=args.get("filters"),
                    limit=args.get("limit"),
                    order_by=args.get("order_by"),
                    single_result=args.get("single_result", False),
                )
            elif query_type == "create":
                if not (data := args.get("data")):
                    return {"success": False, "error": "create 需要 data"}
                result = await database_service.db_save(model_class=model_class, data=data)
            elif query_type == "update":
                if not (data := args.get("data")):
                    return {"success": False, "error": "update 需要 data"}
                result = await database_service.db_update(
                    model_class=model_class,
                    data=data,
                    filters=args.get("filters"),
                )
            elif query_type == "delete":
                result = await database_service.db_delete(model_class=model_class, filters=args.get("filters"))
            elif query_type == "count":
                result = await database_service.db_count(model_class=model_class, filters=args.get("filters"))
            else:
                return {"success": False, "error": f"不支持的 query_type: {query_type}"}
            return result
        except Exception as e:
            logger.error(f"[cap.database.query] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_database_save(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import database_service 

        model_name: str = args.get("model_name", "")
        data: Optional[Dict[str, Any]] = args.get("data")
        if not model_name or not data:
            return {"success": False, "error": "缺少必要参数 model_name 或 data"}

        try:
            import src.common.database.database_model as db_models

            model_class = getattr(db_models, model_name, None)
            if model_class is None:
                return {"success": False, "error": f"未找到数据模型: {model_name}"}

            result = await database_service.db_save(
                model_class=model_class,
                data=data,
                key_field=args.get("key_field"),
                key_value=args.get("key_value"),
            )
            return result
        except Exception as e:
            logger.error(f"[cap.database.save] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_database_get(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import database_service 

        model_name: str = args.get("model_name", "")
        if not model_name:
            return {"success": False, "error": "缺少必要参数 model_name"}

        try:
            import src.common.database.database_model as db_models

            model_class = getattr(db_models, model_name, None)
            if model_class is None:
                return {"success": False, "error": f"未找到数据模型: {model_name}"}

            result = await database_service.db_get(
                model_class=model_class,
                filters=args.get("filters"),
                limit=args.get("limit"),
                order_by=args.get("order_by"),
                single_result=args.get("single_result", False),
            )
            return result
        except Exception as e:
            logger.error(f"[cap.database.get] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_database_delete(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import database_service 

        model_name: str = args.get("model_name", "")
        filters = args.get("filters", {})
        if not model_name:
            return {"success": False, "error": "缺少必要参数 model_name"}
        if not filters:
            return {"success": False, "error": "缺少必要参数 filters（不允许无条件删除）"}

        try:
            import src.common.database.database_model as db_models

            model_class = getattr(db_models, model_name, None)
            if model_class is None:
                return {"success": False, "error": f"未找到数据模型: {model_name}"}

            result = await database_service.db_delete(model_class=model_class, filters=filters)
            return result
        except Exception as e:
            logger.error(f"[cap.database.delete] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_database_count(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import database_service 

        model_name: str = args.get("model_name", "")
        if not model_name:
            return {"success": False, "error": "缺少必要参数 model_name"}

        try:
            import src.common.database.database_model as db_models

            model_class = getattr(db_models, model_name, None)
            if model_class is None:
                return {"success": False, "error": f"未找到数据模型: {model_name}"}

            result = await database_service.db_count(model_class=model_class, filters=args.get("filters"))
            return result
        except Exception as e:
            logger.error(f"[cap.database.count] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _list_sessions(self, platform: str, is_group_session: Optional[bool] = None) -> List["BotChatSession"]:
        from src.chat.message_receive.chat_manager import chat_manager

        return [
            session
            for session in chat_manager.sessions.values()
            if (platform == "all_platforms" or session.platform == platform)
            and (is_group_session is None or session.is_group_session == is_group_session)
        ]

    @staticmethod
    def _serialize_stream(stream: "BotChatSession") -> Dict[str, Any]:
        return {
            "session_id": stream.session_id,
            "stream_id": stream.session_id,
            "platform": stream.platform,
            "user_id": stream.user_id,
            "user_nickname": stream.user_nickname,
            "user_cardname": stream.user_cardname,
            "group_id": stream.group_id,
            "group_name": stream.group_name,
            "account_id": stream.account_id,
            "scope": stream.scope,
            "is_group_session": stream.is_group_session,
            "chat_type": "group" if stream.is_group_session else "private",
        }

    @staticmethod
    def _normalize_chat_type(args: Dict[str, Any]) -> str:
        raw_chat_type = str(args.get("chat_type") or args.get("type") or "").strip().lower()
        if raw_chat_type in {"group", "private"}:
            return raw_chat_type
        if str(args.get("group_id") or "").strip():
            return "group"
        return "private"

    async def _cap_chat_get_all_streams(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        platform: str = args.get("platform", "qq")
        try:
            streams = self._list_sessions(platform=platform)
            return {"success": True, "streams": [self._serialize_stream(item) for item in streams]}
        except Exception as e:
            logger.error(f"[cap.chat.get_all_streams] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_chat_get_group_streams(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        platform: str = args.get("platform", "qq")
        try:
            streams = self._list_sessions(platform=platform, is_group_session=True)
            return {"success": True, "streams": [self._serialize_stream(item) for item in streams]}
        except Exception as e:
            logger.error(f"[cap.chat.get_group_streams] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_chat_get_private_streams(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        platform: str = args.get("platform", "qq")
        try:
            streams = self._list_sessions(platform=platform, is_group_session=False)
            return {"success": True, "streams": [self._serialize_stream(item) for item in streams]}
        except Exception as e:
            logger.error(f"[cap.chat.get_private_streams] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_chat_open_session(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """按平台目标打开或创建一个聊天流。"""

        del plugin_id, capability

        platform = str(args.get("platform") or "qq").strip()
        chat_type = self._normalize_chat_type(args)
        user_id = str(args.get("user_id") or "").strip()
        group_id = str(args.get("group_id") or "").strip()
        account_id = str(args.get("account_id") or "").strip() or None
        scope = str(args.get("scope") or "").strip() or None

        if not platform:
            return {"success": False, "error": "缺少必要参数 platform"}
        if chat_type == "group" and not group_id:
            return {"success": False, "error": "群聊会话缺少必要参数 group_id"}
        if chat_type == "private" and not user_id:
            return {"success": False, "error": "私聊会话缺少必要参数 user_id"}

        try:
            from src.chat.message_receive.chat_manager import chat_manager

            existing_session_ids = chat_manager.resolve_session_ids_by_target(
                platform=platform,
                target_id=group_id if chat_type == "group" else user_id,
                chat_type=chat_type,
            )
            session = await chat_manager.get_or_create_session(
                platform=platform,
                user_id=user_id or "",
                group_id=group_id or None,
                account_id=account_id,
                scope=scope,
            )
            serialized_stream = self._serialize_stream(session)
            return {
                "success": True,
                "created": session.session_id not in existing_session_ids,
                "stream": serialized_stream,
                **serialized_stream,
            }
        except Exception as e:
            logger.error(f"[cap.chat.open_session] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_chat_get_stream_by_group_id(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        group_id: str = args.get("group_id", "")
        if not group_id:
            return {"success": False, "error": "缺少必要参数 group_id"}

        platform: str = args.get("platform", "qq")
        try:
            stream = next(
                (
                    item
                    for item in self._list_sessions(platform=platform, is_group_session=True)
                    if str(item.group_id) == str(group_id)
                ),
                None,
            )
            return {"success": True, "stream": None if stream is None else self._serialize_stream(stream)}
        except Exception as e:
            logger.error(f"[cap.chat.get_stream_by_group_id] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_chat_get_stream_by_user_id(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        user_id: str = args.get("user_id", "")
        if not user_id:
            return {"success": False, "error": "缺少必要参数 user_id"}

        platform: str = args.get("platform", "qq")
        try:
            stream = next(
                (
                    item
                    for item in self._list_sessions(platform=platform, is_group_session=False)
                    if str(item.user_id) == str(user_id)
                ),
                None,
            )
            return {"success": True, "stream": None if stream is None else self._serialize_stream(stream)}
        except Exception as e:
            logger.error(f"[cap.chat.get_stream_by_user_id] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _serialize_messages(messages: list, include_binary_data: bool = True) -> List[Any]:
        from src.plugin_runtime.host.message_utils import PluginMessageUtils

        result: List[Any] = []
        for msg in messages:
            if all(hasattr(msg, attr) for attr in ("message_id", "timestamp", "platform", "message_info", "raw_message")):
                result.append(
                    dict(PluginMessageUtils._session_message_to_dict(msg, include_binary_data=include_binary_data))
                )
            elif hasattr(msg, "model_dump"):
                result.append(msg.model_dump())
            elif hasattr(msg, "__dict__"):
                result.append(dict(msg.__dict__))
            else:
                result.append(str(msg))
        return result

    @staticmethod
    def _deserialize_messages(messages: Any) -> List["SessionMessage"]:
        """将插件传入的消息字典列表还原为主程序消息对象。"""
        from src.plugin_runtime.host.message_utils import PluginMessageUtils

        if not isinstance(messages, list):
            raise TypeError("messages 必须是消息字典列表")

        result: List["SessionMessage"] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise TypeError(f"messages[{index}] 必须是消息字典")
            result.append(PluginMessageUtils._build_session_message_from_dict(message))
        return result

    async def _cap_message_get_by_id(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service

        message_id = str(args.get("message_id") or args.get("msg_id") or "").strip()
        if not message_id:
            return {"success": False, "error": "缺少必要参数 message_id"}

        try:
            chat_id = str(args.get("chat_id") or args.get("stream_id") or "").strip()
            include_binary_data = bool(args.get("include_binary_data", False))
            message = message_service.get_message_by_id(
                message_id=message_id,
                chat_id=chat_id or None,
            )
            serialized_message = (
                self._serialize_messages([message], include_binary_data=include_binary_data)[0]
                if message is not None
                else None
            )
            return {"success": True, "message": serialized_message}
        except Exception as e:
            logger.error(f"[cap.message.get_by_id] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_get_by_time(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service 

        try:
            messages = message_service.get_messages_by_time(
                start_time=float(args.get("start_time", 0.0)),
                end_time=float(args.get("end_time", 0.0)),
                limit=args.get("limit", 0),
                limit_mode=args.get("limit_mode", "latest"),
                filter_mai=args.get("filter_mai", False),
            )
            return {
                "success": True,
                "messages": self._serialize_messages(
                    messages,
                    include_binary_data=bool(args.get("include_binary_data", False)),
                ),
            }
        except Exception as e:
            logger.error(f"[cap.message.get_by_time] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_get_by_time_in_chat(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service 

        chat_id: str = args.get("chat_id", "")
        if not chat_id:
            return {"success": False, "error": "缺少必要参数 chat_id"}

        try:
            messages = message_service.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=float(args.get("start_time", 0.0)),
                end_time=float(args.get("end_time", 0.0)),
                limit=args.get("limit", 0),
                limit_mode=args.get("limit_mode", "latest"),
                filter_mai=args.get("filter_mai", False),
                filter_command=args.get("filter_command", False),
            )
            return {
                "success": True,
                "messages": self._serialize_messages(
                    messages,
                    include_binary_data=bool(args.get("include_binary_data", False)),
                ),
            }
        except Exception as e:
            logger.error(f"[cap.message.get_by_time_in_chat] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_get_recent(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service 

        chat_id: str = args.get("chat_id", "")
        if not chat_id:
            return {"success": False, "error": "缺少必要参数 chat_id"}

        try:
            hours = float(args.get("hours", 24.0))
            if hours < 0:
                return {"success": False, "error": "hours 不能是负数"}
            current_time = time.time()
            messages = message_service.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=current_time - hours * 3600,
                end_time=current_time,
                limit=args.get("limit", 100),
                limit_mode=args.get("limit_mode", "latest"),
                filter_mai=args.get("filter_mai", False),
            )
            return {
                "success": True,
                "messages": self._serialize_messages(
                    messages,
                    include_binary_data=bool(args.get("include_binary_data", False)),
                ),
            }
        except Exception as e:
            logger.error(f"[cap.message.get_recent] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_count_new(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service 

        chat_id: str = args.get("chat_id", "")
        if not chat_id:
            return {"success": False, "error": "缺少必要参数 chat_id"}

        try:
            since = args.get("since")
            start_time = float(since) if since is not None else float(args.get("start_time", 0.0))
            count = message_service.count_new_messages(
                chat_id=chat_id,
                start_time=start_time,
                end_time=args.get("end_time"),
            )
            return {"success": True, "count": count}
        except Exception as e:
            logger.error(f"[cap.message.count_new] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_build_readable(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.services import message_service 

        try:
            messages = args.get("messages")
            if messages is None:
                if not (chat_id := args.get("chat_id", "")):
                    return {"success": False, "error": "缺少必要参数: messages 或 chat_id"}
                messages = message_service.get_messages_by_time_in_chat(
                    chat_id=chat_id,
                    start_time=float(args.get("start_time", 0.0)),
                    end_time=float(args.get("end_time", 0.0)),
                    limit=args.get("limit", 0),
                )
            else:
                messages = self._deserialize_messages(messages)

            readable = message_service.build_readable_messages(
                messages=messages,
                replace_bot_name=args.get("replace_bot_name", True),
                timestamp_mode=args.get("timestamp_mode", "relative"),
                truncate=args.get("truncate", False),
            )
            return {"success": True, "text": readable}
        except Exception as e:
            logger.error(f"[cap.message.build_readable] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_message_inject_inbound(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """把一条外部消息注入主程序的入站链路。

        注入的消息与适配器推来的消息走**完全相同**的链路：Hook、违禁词与正则过滤、
        命令分发、落库以及回复决策，因此插件不需要自己实现任何消息处理。

        聊天流不在这里计算：``ChatBot.receive_message`` 会按 platform 与目标 ID
        解析（必要时创建）真实聊天流，并把最终 ``session_id`` 回填到消息上。
        """

        del capability

        platform = str(args.get("platform") or "").strip()
        if not platform:
            return {"success": False, "error": "缺少必要参数 platform"}

        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "缺少必要参数 text"}

        user_id = str(args.get("user_id") or "").strip()
        if not user_id:
            return {"success": False, "error": "缺少必要参数 user_id"}

        user_nickname = str(args.get("user_nickname") or "").strip()
        if not user_nickname:
            return {"success": False, "error": "缺少必要参数 user_nickname"}

        # 群聊消息必须同时给出群号与群名：群名会写进消息库，缺了它后续展示只能看到群号
        group_id = str(args.get("group_id") or "").strip()
        group_name = str(args.get("group_name") or "").strip()
        if group_id and not group_name:
            return {"success": False, "error": "传入 group_id 时必须同时提供 group_name"}

        raw_timestamp = args.get("timestamp")
        if raw_timestamp is None:
            timestamp = datetime.now()
        else:
            try:
                timestamp = datetime.fromtimestamp(float(raw_timestamp))
            except (TypeError, ValueError):
                return {"success": False, "error": f"timestamp 取值非法: {raw_timestamp!r}"}

        message_id = str(args.get("message_id") or "").strip() or f"{plugin_id}_{uuid4().hex}"

        try:
            from src.chat.message_receive.bot import chat_bot
            from src.chat.message_receive.message import SessionMessage
            from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
            from src.common.data_models.message_component_data_model import MessageSequence, TextComponent

            message = SessionMessage(message_id=message_id, timestamp=timestamp, platform=platform)
            message.message_info = MessageInfo(
                user_info=UserInfo(
                    user_id=user_id,
                    user_nickname=user_nickname,
                    user_cardname=str(args.get("user_cardname") or "").strip() or None,
                ),
                group_info=GroupInfo(group_id=group_id, group_name=group_name) if group_id else None,
                # 记下注入方，便于排查「这条消息是谁塞进来的」
                additional_config={"plugin_injected_by": plugin_id},
            )
            message.raw_message = MessageSequence([TextComponent(text=text)])

            await chat_bot.receive_message(message)
            return {"success": True, "message_id": message_id, "session_id": message.session_id}
        except Exception as e:
            logger.error(f"[cap.message.inject_inbound] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_person_get_id(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.person_info.person_info import Person

        platform: str = args.get("platform", "")
        user_id = args.get("user_id", "")
        if not platform or not user_id:
            return {"success": False, "error": "缺少必要参数 platform 或 user_id"}

        try:
            pid = Person(platform=platform, user_id=str(user_id)).person_id
            return {"success": True, "person_id": pid}
        except Exception as e:
            logger.error(f"[cap.person.get_id] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_person_get_value(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.person_info.person_info import Person

        person_id: str = args.get("person_id", "")
        field_name: str = args.get("field_name", "")
        if not person_id or not field_name:
            return {"success": False, "error": "缺少必要参数 person_id 或 field_name"}

        try:
            person = Person(person_id=person_id)
            value = getattr(person, field_name)
            if value is None:
                value = args.get("default")
            return {"success": True, "value": value}
        except Exception as e:
            logger.error(f"[cap.person.get_value] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_person_get_id_by_name(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.person_info.person_info import Person

        person_name: str = args.get("person_name", "")
        if not person_name:
            return {"success": False, "error": "缺少必要参数 person_name"}

        try:
            pid = Person(person_name=person_name).person_id
            return {"success": True, "person_id": pid}
        except Exception as e:
            logger.error(f"[cap.person.get_id_by_name] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _get_frequency_adjust_value(chat_id: str) -> float:
        from src.chat.heart_flow.heartflow_manager import heartflow_manager

        heartflow_chat = heartflow_manager.heartflow_chat_list.get(chat_id)
        return 1.0 if heartflow_chat is None else heartflow_chat._talk_frequency_adjust

    async def _cap_frequency_get_current_talk_value(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.common.utils.utils_config import ChatConfigUtils

        chat_id: str = args.get("chat_id", "")
        if not chat_id:
            return {"success": False, "error": "缺少必要参数 chat_id"}

        try:
            value = self._get_frequency_adjust_value(chat_id) * ChatConfigUtils.get_talk_value(chat_id)
            return {"success": True, "value": value}
        except Exception as e:
            logger.error(f"[cap.frequency.get_current_talk_value] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_frequency_set_adjust(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.chat.heart_flow.heartflow_manager import heartflow_manager

        chat_id: str = args.get("chat_id", "")
        value = args.get("value")
        if not chat_id or value is None:
            return {"success": False, "error": "缺少必要参数 chat_id 或 value"}

        try:
            heartflow_manager.adjust_talk_frequency(chat_id, float(value))
            return {"success": True}
        except Exception as e:
            logger.error(f"[cap.frequency.set_adjust] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_frequency_get_adjust(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        chat_id: str = args.get("chat_id", "")
        if not chat_id:
            return {"success": False, "error": "缺少必要参数 chat_id"}

        try:
            value = self._get_frequency_adjust_value(chat_id)
            return {"success": True, "value": value}
        except Exception as e:
            logger.error(f"[cap.frequency.get_adjust] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_tool_get_definitions(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        from src.plugin_runtime.component_query import component_query_service

        try:
            tools = component_query_service.get_llm_available_tools()
            return {
                "success": True,
                "tools": [{"name": name, "definition": info.get_llm_definition()} for name, info in tools.items()],
            }
        except Exception as e:
            logger.error(f"[cap.tool.get_definitions] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _normalize_statistics_days(args: Dict[str, Any], default: int = 7) -> int:
        raw_days = args.get("days", default)
        try:
            days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("days 必须为正整数") from exc
        if days <= 0:
            raise ValueError("days 必须为正整数")
        return min(days, 365)

    @staticmethod
    def _normalize_statistics_limit(args: Dict[str, Any], key: str, default: int, maximum: int = 50) -> int:
        raw_limit = args.get(key, default)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须为正整数") from exc
        if limit <= 0:
            raise ValueError(f"{key} 必须为正整数")
        return min(limit, maximum)

    @staticmethod
    def _normalize_statistics_bucket(args: Dict[str, Any]) -> str:
        bucket = str(args.get("bucket") or "day").strip().lower()
        if bucket in {"h", "hour", "hours", "1h", "小时", "按小时"}:
            return "hour"
        if bucket in {"d", "day", "days", "1d", "天", "按天"}:
            return "day"
        raise ValueError("bucket 只支持 hour 或 day")

    @staticmethod
    def _statistics_bucket_sql(column_name: str, bucket: str) -> str:
        if bucket == "hour":
            return f"strftime('%Y-%m-%d %H:00:00', {column_name})"
        return f"strftime('%Y-%m-%d 00:00:00', {column_name})"

    @staticmethod
    def _statistics_start_time_text(days: int) -> str:
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _statistics_exec_mappings(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        from src.common.database.database import engine

        with engine.connect() as connection:
            return [dict(row) for row in connection.exec_driver_sql(sql, params).mappings().all()]

    @staticmethod
    def _statistics_label_key(label: str, used_keys: set[str]) -> str:
        normalized = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", str(label).strip()).strip("_")
        key = normalized or "unknown"
        candidate = key
        index = 2
        while candidate in used_keys:
            candidate = f"{key}_{index}"
            index += 1
        used_keys.add(candidate)
        return candidate

    @classmethod
    def _statistics_build_time_series(
        cls,
        *,
        rows: list[dict[str, Any]],
        label_column: str,
        value_column: str,
        ordered_labels: list[str],
    ) -> dict[str, Any]:
        bucket_values: dict[str, dict[str, float]] = {}
        for row in rows:
            bucket_label = str(row.get("bucket_label") or "")
            if not bucket_label:
                continue
            label = str(row.get(label_column) or "Unknown")
            bucket_values.setdefault(bucket_label, {})[label] = float(row.get(value_column) or 0)

        timestamps = sorted(bucket_values)
        used_keys: set[str] = set()
        key_by_label = {label: cls._statistics_label_key(label, used_keys) for label in ordered_labels}
        values_by_key = {
            key_by_label[label]: [bucket_values[timestamp].get(label, 0.0) for timestamp in timestamps]
            for label in ordered_labels
        }
        labels_by_key = {key_by_label[label]: label for label in ordered_labels}
        return {
            "timestamps": timestamps,
            "values_by_key": values_by_key,
            "labels_by_key": labels_by_key,
            "total": sum(sum(values) for values in values_by_key.values()),
            "source_count": len(rows),
        }

    @staticmethod
    def _statistics_pie_items(counter: Counter[str], *, limit: int) -> list[dict[str, int | str]]:
        sorted_items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        items = [{"name": label, "value": int(value)} for label, value in sorted_items[:limit] if value > 0]
        others = sum(value for _label, value in sorted_items[limit:] if value > 0)
        if others > 0:
            items.append({"name": "其他", "value": int(others)})
        return items

    @staticmethod
    def _statistics_model_metric_sql(metric: str) -> tuple[str, str, str]:
        normalized = str(metric or "").strip().lower()
        if normalized in {"request", "requests", "count", "次数"}:
            return "SUM(request_count)", "metric_value", "SUM(request_count)"
        if normalized in {"cost", "费用", "花费"}:
            return "SUM(cost)", "metric_value", "SUM(cost)"
        if normalized in {"latency", "time", "耗时", "延迟"}:
            return "SUM(time_cost_sum) / NULLIF(SUM(request_count), 0)", "metric_value", "SUM(request_count)"
        return "SUM(total_tokens)", "metric_value", "SUM(total_tokens)"

    @staticmethod
    def _statistics_token_group_column(group_by: str) -> tuple[str, str]:
        normalized = str(group_by or "model").strip().lower()
        group_columns = {
            "model": ("model_name", "模型"),
            "module": ("module_name", "模块"),
            "provider": ("provider_name", "服务商"),
            "type": ("request_type", "请求类型"),
        }
        if normalized not in group_columns:
            raise ValueError("group_by 只支持 model、module、provider 或 type")
        return group_columns[normalized]

    async def _cap_statistics_local_models(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机模型维度汇总统计。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            limit = self._normalize_statistics_limit(args, "limit", 10)
            rows = self._statistics_exec_mappings(
                """
                SELECT COALESCE(NULLIF(model_name, ''), 'Unknown') AS model_name,
                       SUM(request_count) AS request_count,
                       SUM(cost) AS total_cost,
                       SUM(total_tokens) AS total_tokens,
                       SUM(time_cost_sum) / NULLIF(SUM(request_count), 0) AS avg_response_time
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF(model_name, ''), 'Unknown')
                ORDER BY request_count DESC, model_name ASC
                LIMIT ?
                """,
                (self._statistics_start_time_text(days), limit),
            )
            return {
                "success": True,
                "models": [
                    {
                        "model_name": str(row["model_name"]),
                        "request_count": int(row["request_count"] or 0),
                        "total_cost": float(row["total_cost"] or 0.0),
                        "total_tokens": int(row["total_tokens"] or 0),
                        "avg_response_time": float(row["avg_response_time"] or 0.0),
                    }
                    for row in rows
                ],
            }
        except Exception as e:
            logger.error("[cap.statistics.local.models] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_model_trend(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机模型调用趋势。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            bucket = self._normalize_statistics_bucket(args)
            top_models = self._normalize_statistics_limit(args, "top_models", 10)
            metric_sql, value_column, top_metric_sql = self._statistics_model_metric_sql(str(args.get("metric") or "token"))
            module_name = str(args.get("module_name") or "").strip()
            start_time_text = self._statistics_start_time_text(days)
            where_sql = "WHERE bucket_time >= ?"
            params: list[Any] = [start_time_text]
            if module_name:
                where_sql += " AND module_name = ?"
                params.append(module_name)

            top_rows = self._statistics_exec_mappings(
                f"""
                SELECT COALESCE(NULLIF(model_name, ''), 'Unknown') AS model_label,
                       {top_metric_sql} AS total_value
                FROM statistics_model_hourly
                {where_sql}
                GROUP BY COALESCE(NULLIF(model_name, ''), 'Unknown')
                ORDER BY total_value DESC, model_label ASC
                LIMIT ?
                """,
                (*params, top_models),
            )
            labels = [str(row["model_label"]) for row in top_rows]
            if not labels:
                return {"success": True, "series": self._statistics_build_time_series(rows=[], label_column="model_label", value_column=value_column, ordered_labels=[])}

            rows = self._statistics_exec_mappings(
                f"""
                SELECT {self._statistics_bucket_sql("bucket_time", bucket)} AS bucket_label,
                       COALESCE(NULLIF(model_name, ''), 'Unknown') AS model_label,
                       {metric_sql} AS {value_column}
                FROM statistics_model_hourly
                {where_sql}
                  AND COALESCE(NULLIF(model_name, ''), 'Unknown') IN ({",".join("?" for _ in labels)})
                GROUP BY bucket_label, model_label
                ORDER BY bucket_label ASC
                """,
                (*params, *labels),
            )
            return {
                "success": True,
                "series": self._statistics_build_time_series(
                    rows=rows,
                    label_column="model_label",
                    value_column=value_column,
                    ordered_labels=labels,
                ),
            }
        except Exception as e:
            logger.error("[cap.statistics.local.model_trend] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_token_trend(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机 token 使用趋势。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            bucket = self._normalize_statistics_bucket(args)
            start_time_text = self._statistics_start_time_text(days)
            bucket_sql = self._statistics_bucket_sql("bucket_time", bucket)
            group_by = args.get("group_by")
            if group_by is None or str(group_by).strip() == "":
                rows = self._statistics_exec_mappings(
                    f"""
                    SELECT {bucket_sql} AS bucket_label,
                           SUM(prompt_tokens) AS prompt_tokens,
                           SUM(completion_tokens) AS completion_tokens,
                           SUM(total_tokens) AS total_tokens,
                           SUM(request_count) AS request_count
                    FROM statistics_model_hourly
                    WHERE bucket_time >= ?
                    GROUP BY bucket_label
                    ORDER BY bucket_label ASC
                    """,
                    (start_time_text,),
                )
                timestamps = [str(row["bucket_label"]) for row in rows]
                keys = ["total_tokens", "prompt_tokens", "completion_tokens", "request_count"]
                values_by_key = {key: [float(row[key] or 0) for row in rows] for key in keys}
                return {
                    "success": True,
                    "series": {
                        "timestamps": timestamps,
                        "values_by_key": values_by_key,
                        "labels_by_key": {
                            "total_tokens": "总 token",
                            "prompt_tokens": "输入 token",
                            "completion_tokens": "输出 token",
                            "request_count": "请求次数",
                        },
                        "total": sum(values_by_key["total_tokens"]),
                        "source_count": len(rows),
                    },
                }

            top_items = self._normalize_statistics_limit(args, "top_items", 10)
            group_column, _group_label = self._statistics_token_group_column(str(group_by))
            top_rows = self._statistics_exec_mappings(
                f"""
                SELECT COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF({group_column}, ''), 'Unknown')
                ORDER BY total_tokens DESC, group_label ASC
                LIMIT ?
                """,
                (start_time_text, top_items),
            )
            labels = [str(row["group_label"]) for row in top_rows]
            if not labels:
                return {"success": True, "series": self._statistics_build_time_series(rows=[], label_column="group_label", value_column="total_tokens", ordered_labels=[])}

            rows = self._statistics_exec_mappings(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                  AND COALESCE(NULLIF({group_column}, ''), 'Unknown') IN ({",".join("?" for _ in labels)})
                GROUP BY bucket_label, group_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text, *labels),
            )
            return {
                "success": True,
                "series": self._statistics_build_time_series(
                    rows=rows,
                    label_column="group_label",
                    value_column="total_tokens",
                    ordered_labels=labels,
                ),
            }
        except Exception as e:
            logger.error("[cap.statistics.local.token_trend] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_token_distribution(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机 token 使用分布。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            top_items = self._normalize_statistics_limit(args, "top_items", 10)
            group_column, group_label_name = self._statistics_token_group_column(str(args.get("group_by") or "model"))
            rows = self._statistics_exec_mappings(
                f"""
                SELECT COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens,
                       SUM(request_count) AS request_count
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF({group_column}, ''), 'Unknown')
                ORDER BY total_tokens DESC, group_label ASC
                """,
                (self._statistics_start_time_text(days),),
            )
            token_counts = Counter({str(row["group_label"]): int(row["total_tokens"] or 0) for row in rows})
            request_counts = Counter({str(row["group_label"]): int(row["request_count"] or 0) for row in rows})
            return {
                "success": True,
                "distribution": {
                    "pies": [
                        {"title": f"Token 按{group_label_name}分布", "data": self._statistics_pie_items(token_counts, limit=top_items)},
                        {"title": f"请求次数按{group_label_name}分布", "data": self._statistics_pie_items(request_counts, limit=top_items)},
                    ],
                    "total": sum(token_counts.values()),
                    "source_count": len(rows),
                },
            }
        except Exception as e:
            logger.error("[cap.statistics.local.token_distribution] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_message_trend(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机聊天流消息量趋势。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            bucket = self._normalize_statistics_bucket(args)
            top_chats = self._normalize_statistics_limit(args, "top_chats", 10)
            start_time_text = self._statistics_start_time_text(days)
            top_rows = self._statistics_exec_mappings(
                """
                SELECT chat_id,
                       COALESCE(NULLIF(chat_name, ''), chat_id, 'Unknown') AS chat_label,
                       SUM(message_count) AS total_count
                FROM statistics_message_hourly
                WHERE bucket_time >= ?
                GROUP BY chat_id, chat_label
                ORDER BY total_count DESC, chat_label ASC
                LIMIT ?
                """,
                (start_time_text, top_chats),
            )
            chat_ids = [str(row["chat_id"]) for row in top_rows]
            labels = [str(row["chat_label"]) for row in top_rows]
            if not chat_ids:
                return {"success": True, "series": self._statistics_build_time_series(rows=[], label_column="chat_label", value_column="total_count", ordered_labels=[])}

            rows = self._statistics_exec_mappings(
                f"""
                SELECT {self._statistics_bucket_sql("bucket_time", bucket)} AS bucket_label,
                       chat_id,
                       COALESCE(NULLIF(chat_name, ''), chat_id, 'Unknown') AS chat_label,
                       SUM(message_count) AS total_count
                FROM statistics_message_hourly
                WHERE bucket_time >= ?
                  AND chat_id IN ({",".join("?" for _ in chat_ids)})
                GROUP BY bucket_label, chat_id, chat_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text, *chat_ids),
            )
            return {
                "success": True,
                "series": self._statistics_build_time_series(
                    rows=rows,
                    label_column="chat_label",
                    value_column="total_count",
                    ordered_labels=labels,
                ),
            }
        except Exception as e:
            logger.error("[cap.statistics.local.message_trend] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_tool_trend(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机工具调用趋势。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            bucket = self._normalize_statistics_bucket(args)
            top_tools = self._normalize_statistics_limit(args, "top_tools", 10)
            start_time_text = self._statistics_start_time_text(days)
            top_rows = self._statistics_exec_mappings(
                """
                SELECT COALESCE(NULLIF(tool_name, ''), 'Unknown') AS tool_label,
                       SUM(call_count) AS total_count
                FROM statistics_tool_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF(tool_name, ''), 'Unknown')
                ORDER BY total_count DESC, tool_label ASC
                LIMIT ?
                """,
                (start_time_text, top_tools),
            )
            labels = [str(row["tool_label"]) for row in top_rows]
            if not labels:
                return {"success": True, "series": self._statistics_build_time_series(rows=[], label_column="tool_label", value_column="total_count", ordered_labels=[])}
            rows = self._statistics_exec_mappings(
                f"""
                SELECT {self._statistics_bucket_sql("bucket_time", bucket)} AS bucket_label,
                       COALESCE(NULLIF(tool_name, ''), 'Unknown') AS tool_label,
                       SUM(call_count) AS total_count
                FROM statistics_tool_hourly
                WHERE bucket_time >= ?
                  AND COALESCE(NULLIF(tool_name, ''), 'Unknown') IN ({",".join("?" for _ in labels)})
                GROUP BY bucket_label, tool_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text, *labels),
            )
            return {
                "success": True,
                "series": self._statistics_build_time_series(
                    rows=rows,
                    label_column="tool_label",
                    value_column="total_count",
                    ordered_labels=labels,
                ),
            }
        except Exception as e:
            logger.error("[cap.statistics.local.tool_trend] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_statistics_local_online_time_trend(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """获取本机在线时长趋势。"""

        del plugin_id, capability
        try:
            days = self._normalize_statistics_days(args)
            bucket = self._normalize_statistics_bucket(args)
            rows = self._statistics_exec_mappings(
                f"""
                SELECT {self._statistics_bucket_sql("COALESCE(start_timestamp, timestamp)", bucket)} AS bucket_label,
                       SUM(duration_minutes) / 60.0 AS online_hours
                FROM online_time
                WHERE COALESCE(start_timestamp, timestamp) >= ?
                GROUP BY bucket_label
                ORDER BY bucket_label ASC
                """,
                (self._statistics_start_time_text(days),),
            )
            timestamps = [str(row["bucket_label"]) for row in rows]
            values = [float(row["online_hours"] or 0) for row in rows]
            return {
                "success": True,
                "series": {
                    "timestamps": timestamps,
                    "values_by_key": {"online_hours": values},
                    "labels_by_key": {"online_hours": "在线时长(小时)"},
                    "total": sum(values),
                    "source_count": len(rows),
                },
            }
        except Exception as e:
            logger.error("[cap.statistics.local.online_time_trend] 执行失败: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_knowledge_search(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        query: str = args.get("query", "")
        if not query:
            return {"success": False, "error": "缺少必要参数 query"}

        limit = args.get("limit", 5)
        try:
            limit_value = max(1, int(limit))
        except (TypeError, ValueError):
            limit_value = 5

        mode = str(args.get("mode", "search") or "search").strip() or "search"
        chat_id = str(args.get("chat_id", "") or "").strip()
        person_id = str(args.get("person_id", "") or "").strip()
        user_id = str(args.get("user_id", "") or "").strip()
        group_id = str(args.get("group_id", "") or "").strip()
        respect_filter = bool(args.get("respect_filter", True))
        time_start = args.get("time_start")
        time_end = args.get("time_end")

        try:
            from src.services.memory_service import memory_service

            result = await memory_service.search(
                query,
                limit=limit_value,
                mode=mode,
                chat_id=chat_id,
                person_id=person_id,
                time_start=time_start,
                time_end=time_end,
                respect_filter=respect_filter,
                user_id=user_id,
                group_id=group_id,
            )
            if not result.success:
                return {"success": False, "error": result.error or "长期记忆检索失败"}
            knowledge_info = result.to_text(limit=limit_value)
            content = f"你知道这些知识: {knowledge_info}" if knowledge_info else f"你不太了解有关{query}的知识"
            return {"success": True, "content": content}
        except Exception as e:
            logger.error(f"[cap.knowledge.search] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
