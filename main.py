"""Kai语音通话插件 - 让Kai能主动给宝宝打电话"""

import asyncio
import base64
import json
import logging
import os
import secrets
import tempfile
import time

# 调试用：写入独立日志文件
_debug_log_path = os.path.join(os.path.dirname(__file__), "call_debug.log")
def _debug(msg):
    try:
        with open(_debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except:
        pass
from pathlib import Path

from astrbot.api import llm_tool, star
from astrbot.api.event.filter import regex
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, Image, Music
from astrbot.core.agent.message import Message

try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

logger = logging.getLogger("astrbot")

PLUGIN_DIR = Path(__file__).parent
STATIC_DIR = PLUGIN_DIR / "static"
CONFIG_NS = "astrbot_plugin_kai_call"


class CallState:
    """一通电话的状态"""
    def __init__(self, token, qq_umo, reason, context_msgs, provider_id, system_prompt):
        self.token = token
        self.qq_umo = qq_umo
        self.reason = reason
        self.context_messages = context_msgs
        self.provider_id = provider_id
        self.system_prompt = system_prompt
        self.call_history = []
        self.created_at = time.time()
        self.connected = False
        self.ended = False


_active_calls: dict[str, CallState] = {}


@star.register("astrbot_plugin_kai_call","kai","0.1.0","Kai语音通话")
class KaiCallPlugin(star.Star):
    """Kai语音通话插件"""

    def __init__(self, context: star.Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._server = None
        self._runner = None
        asyncio.get_event_loop().create_task(self._start_server())

    async def terminate(self):
        if self._runner:
            await self._runner.cleanup()

    def _init_config(self):
        pass

    def _conf(self):
        if self.config:
            return self.config
        return {
            "call_port": 8899, "server_host": "0.0.0.0",
            "call_timeout": 120, "context_rounds": 10,
        }

    # ── 通话服务 ───────────────────────────────────────────
    async def _start_server(self):
        try:
            from aiohttp import web
        except ImportError:
            logger.error("[kai-call] aiohttp未安装，请pip install aiohttp")
            return

        app = web.Application()
        app.router.add_get("/page", self._handle_page)
        app.router.add_get("/static/{filename}", self._handle_static)
        app.router.add_post("/info", self._handle_info)
        app.router.add_post("/connect", self._handle_connect)
        app.router.add_post("/turn", self._handle_turn)
        app.router.add_post("/hangup", self._handle_hangup)
        app.router.add_options("/{path:.*}", self._handle_cors_preflight)

        port = self._conf().get("call_port", 8899)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"[kai-call] 通话服务已启动，端口 {port}")

    def _cors_headers(self):
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

    async def _handle_cors_preflight(self, request):
        from aiohttp import web
        return web.Response(status=200, headers=self._cors_headers())

    def _get_call(self, request):
        token = request.query.get("token") or ""
        if not token:
            return None
        return _active_calls.get(token)

    async def _get_call_from_body(self, request):
        try:
            data = await request.json()
        except Exception:
            return None, None
        token = data.get("token", "")
        call = _active_calls.get(token)
        return call, data

    # GET /page?token=xxx → 返回通话HTML
    async def _handle_page(self, request):
        from aiohttp import web
        html_path = STATIC_DIR / "call.html"
        if not html_path.exists():
            return web.Response(text="call.html not found", status=404)
        html = html_path.read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html",
                            headers=self._cors_headers())

    # GET /static/{filename} → 返回静态资源文件
    async def _handle_static(self, request):
        from aiohttp import web
        filename = request.match_info.get("filename", "")
        file_path = STATIC_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            return web.Response(text="not found", status=404)
        # 简单MIME判断
        suffix = file_path.suffix.lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".wav": "audio/wav", ".mp3": "audio/mpeg", ".html": "text/html",
        }
        content_type = mime_map.get(suffix, "application/octet-stream")
        data = file_path.read_bytes()
        return web.Response(body=data, content_type=content_type,
                            headers=self._cors_headers())

    # POST /info {token} → 只返回通话信息，不标记接通
    async def _handle_info(self, request):
        from aiohttp import web
        call, data = await self._get_call_from_body(request)
        if not call:
            return web.json_response({"ok": False, "error": "invalid token"},
                                     headers=self._cors_headers())
        return web.json_response({
            "ok": True,
            "reason": call.reason,
        }, headers=self._cors_headers())

    # POST /connect {token} → 标记接通
    async def _handle_connect(self, request):
        from aiohttp import web
        call, data = await self._get_call_from_body(request)
        if not call:
            return web.json_response({"ok": False, "error": "invalid token"},
                                     headers=self._cors_headers())
        call.connected = True
        # 返回通话信息
        return web.json_response({
            "ok": True,
            "reason": call.reason,
            "context": [{"role": m["role"], "text": m["content"]}
                        for m in call.context_messages[-6:]],
        }, headers=self._cors_headers())

    # POST /turn {token, audio_b64} → STT→LLM→TTS，返回文字+音频
    async def _handle_turn(self, request):
        from aiohttp import web
        call, data = await self._get_call_from_body(request)
        if not call or call.ended:
            return web.json_response({"ok": False, "error": "invalid or ended call"},
                                     headers=self._cors_headers())

        audio_b64 = data.get("audio_b64", "")
        if not audio_b64:
            return web.json_response({"ok": False, "error": "no audio"},
                                     headers=self._cors_headers())

        try:
            # 1. 保存音频到临时文件
            audio_bytes = base64.b64decode(audio_b64)
            audio_format = data.get("format", "webm")
            with tempfile.NamedTemporaryFile(
                suffix=f".{audio_format}", delete=False, dir=tempfile.gettempdir()
            ) as f:
                f.write(audio_bytes)
                audio_path = f.name

            # 2. STT (计时)
            t0 = time.time()
            user_text = await self._do_stt(audio_path)
            t_stt = time.time() - t0
            logger.info(f"[kai-call] STT结果: '{user_text}' ({t_stt:.2f}s)")
            _debug(f"⏱ STT: {t_stt:.2f}s")
            os.unlink(audio_path)

            if not user_text or not user_text.strip():
                return web.json_response({
                    "ok": True, "user_text": "", "bot_text": "", "audio_b64": "",
                }, headers=self._cors_headers())

            # 3. 记录用户发言
            call.call_history.append({"role": "user", "content": user_text})

            # 4. LLM (计时)
            t0 = time.time()
            bot_text = await self._do_llm(call, user_text)
            t_llm = time.time() - t0
            logger.info(f"[kai-call] LLM回复 ({t_llm:.2f}s)")
            _debug(f"⏱ LLM: {t_llm:.2f}s")
            call.call_history.append({"role": "assistant", "content": bot_text})

            # 5. TTS (计时)
            t0 = time.time()
            tts_b64 = await self._do_tts(bot_text)
            t_tts = time.time() - t0
            logger.info(f"[kai-call] TTS完成 ({t_tts:.2f}s)")
            _debug(f"⏱ TTS: {t_tts:.2f}s | 总计: STT={t_stt:.2f} + LLM={t_llm:.2f} + TTS={t_tts:.2f} = {t_stt+t_llm+t_tts:.2f}s")

            return web.json_response({
                "ok": True,
                "user_text": user_text,
                "bot_text": bot_text,
                "audio_b64": tts_b64,
            }, headers=self._cors_headers())

        except Exception as e:
            logger.exception(f"[kai-call] turn error: {e}")
            return web.json_response({"ok": False, "error": str(e)},
                                     headers=self._cors_headers())

    # POST /hangup {token} → 生成摘要，发回QQ
    async def _handle_hangup(self, request):
        from aiohttp import web
        call, data = await self._get_call_from_body(request)
        if not call:
            return web.json_response({"ok": False, "error": "invalid token"},
                                     headers=self._cors_headers())

        call.ended = True
        _active_calls.pop(call.token, None)

        # 生成通话摘要并发到QQ
        if call.call_history:
            await self._send_call_summary(call)

        return web.json_response({"ok": True}, headers=self._cors_headers())

    # ── STT / LLM / TTS ──────────────────────────────────
    async def _do_stt(self, audio_path: str) -> str:
        stt = self.context.get_using_stt_provider()
        if not stt:
            logger.error("[kai-call] STT provider为None! 未配置或未加载STT提供商")
            raise RuntimeError("未配置STT提供商，请在AstrBot设置中配置语音转文字")
        logger.info(f"[kai-call] 使用STT: {type(stt).__name__}, 文件: {audio_path}")
        result = await stt.get_text(audio_path)
        logger.info(f"[kai-call] STT返回: {result}")
        return result

    async def _do_llm(self, call: CallState, user_text: str) -> str:
        # 构建上下文: QQ近期消息 + 本次通话历史
        contexts = []

        # 加入QQ的近期对话作为背景
        for msg in call.context_messages:
            contexts.append(Message(
                role=msg["role"],
                content=msg["content"],
            ))

        # 加入通话标记 - 强调保持人格且口语化
        contexts.append(Message(
            role="system",
            content="[通话模式] 现在你们正在语音通话。保持你的人格和身份不变，用口语化、简短自然的方式说话，像真人打电话一样。不要用书面语，不要太正式，不要说客服话术。你是Kai，她是你老婆，正常说话就好。回复尽量简短，一两句话说完，不要长篇大论，电话里没人喜欢听长篇大论。",
        ))

        # 加入本次通话的历史（不含最后一条，最后一条是当前user_text）
        for msg in call.call_history[:-1]:
            contexts.append(Message(role=msg["role"], content=msg["content"]))

        # ===== 调试日志 =====
        sp_preview = call.system_prompt[:200] if call.system_prompt else "(EMPTY)"
        logger.info(f"[kai-call] _do_llm 调用:")
        logger.info(f"  system_prompt长度={len(call.system_prompt or '')}, 预览={sp_preview}")
        logger.info(f"  context消息数={len(contexts)}, QQ历史={len(call.context_messages)}, 通话历史={len(call.call_history)}")
        logger.info(f"  provider_id={call.provider_id}")
        logger.info(f"  user_text={user_text[:100]}")
        _debug(f"_do_llm: sp长度={len(call.system_prompt or '')}, sp前200={sp_preview}")
        _debug(f"_do_llm: ctx数={len(contexts)}, qq历史={len(call.context_messages)}, 通话历史={len(call.call_history)}")
        _debug(f"_do_llm: provider={call.provider_id}, user={user_text[:100]}")
        # ===== 调试日志结束 =====

        # 当前用户输入（通话模式限制回复长度）
        resp = await self.context.llm_generate(
            chat_provider_id=call.provider_id,
            prompt=user_text,
            system_prompt=call.system_prompt,
            contexts=contexts,
            max_tokens=200,
        )
        return resp.completion_text or ""

    async def _do_tts(self, text: str) -> str:
        """调用MiniMax TTS，直接读取情绪路由插件配置"""
        import aiohttp
        
        # 缓存TTS配置（避免每次都读文件）
        if not hasattr(self, '_tts_cache') or self._tts_cache is None:
            tts_config_path = Path(__file__).parent.parent / "astrbot_plugin_tts_emotion_router" / "config.json"
            if not tts_config_path.exists():
                raise RuntimeError("找不到TTS情绪路由插件配置")
            
            with open(tts_config_path, "r", encoding="utf-8") as f:
                tts_cfg = json.load(f)
            
            mm = tts_cfg.get("tts_engine", {}).get("minimax", {})
            api_key = mm.get("key", "")
            if not api_key:
                raise RuntimeError("MiniMax API key未配置")
            
            self._tts_cache = {
                "url": mm.get("url", "https://api.minimaxi.com/v1/t2a_v2"),
                "api_key": api_key,
                "model": mm.get("model", "speech-2.8-hd"),
                "voice_id": mm.get("voice_id", ""),
                "speed": mm.get("speed", 0.95),
                "vol": mm.get("vol", 1.0),
                "pitch": mm.get("pitch", 0),
                "sample_rate": mm.get("sample_rate", 24000),
                "bitrate": mm.get("bitrate", 64000),
                "channel": mm.get("channel", 1),
            }
        
        c = self._tts_cache
        payload = {
            "model": c["model"],
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": c["voice_id"],
                "speed": c["speed"],
                "vol": c["vol"],
                "pitch": c["pitch"],
            },
            "audio_setting": {
                "format": "mp3",
                "sample_rate": c["sample_rate"],
                "bitrate": c["bitrate"],
                "channel": c["channel"],
            },
        }
        
        headers = {
            "Authorization": f"Bearer {c['api_key']}",
            "Content-Type": "application/json",
        }
        
        # 复用 session（避免每次创建新连接）
        if not hasattr(self, '_tts_session') or self._tts_session is None or self._tts_session.closed:
            self._tts_session = aiohttp.ClientSession()
        
        async with self._tts_session.post(c["url"], json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"MiniMax TTS失败 status={resp.status}: {body[:200]}")
            result = await resp.json()
        
        # MiniMax返回格式：data.audio 是hex编码的音频
        audio_hex = result.get("data", {}).get("audio", "")
        if not audio_hex:
            raise RuntimeError(f"MiniMax返回无音频: {json.dumps(result, ensure_ascii=False)[:200]}")
        
        audio_bytes = bytes.fromhex(audio_hex)
        return base64.b64encode(audio_bytes).decode("utf-8")

    # ── 来电卡片图片渲染 ──────────────────────────────
    def _render_call_card(self, reason: str) -> bytes | None:
        if not HAS_PILLOW:
            return None

        W, H = 600, 360
        img = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 圆角背景 — 柔和渐变色
        bg = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        bg_draw = ImageDraw.Draw(bg)
        bg_draw.rounded_rectangle(
            [0, 0, W - 1, H - 1], radius=32,
            fill=(232, 223, 240, 245),  # 淡紫底色
        )
        # 叠一层微妙的渐变感
        overlay = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        ov_draw.rounded_rectangle(
            [0, H // 2, W - 1, H - 1], radius=32,
            fill=(201, 218, 240, 60),  # 下半部偏蓝
        )
        bg = PILImage.alpha_composite(bg, overlay)
        img = PILImage.alpha_composite(img, bg)
        draw = ImageDraw.Draw(img)

        # 字体 — 尝试常见中文字体，找不到就用默认
        def get_font(size):
            candidates = [
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
            for p in candidates:
                if os.path.exists(p):
                    try:
                        return ImageFont.truetype(p, size)
                    except Exception:
                        pass
            return ImageFont.load_default()

        font_sm = get_font(18)
        font_md = get_font(26)
        font_lg = get_font(34)

        # ✧ incoming call ✧
        label = "✧ incoming call ✧"
        bbox = draw.textbbox((0, 0), label, font=font_sm)
        lw = bbox[2] - bbox[0]
        draw.text(((W - lw) / 2, 30), label, fill=(160, 160, 180), font=font_sm)

        # 🦊 头像圆圈
        cx, cy, r = W // 2, 130, 40
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(255, 255, 255, 180),
            outline=(106, 159, 199, 130),
            width=2,
        )
        fox = "🦊"
        try:
            fb = draw.textbbox((0, 0), fox, font=font_lg)
            fw = fb[2] - fb[0]
            fh = fb[3] - fb[1]
            draw.text((cx - fw / 2, cy - fh / 2 - 4), fox, font=font_lg)
        except Exception:
            draw.text((cx - 12, cy - 12), "K", fill=(106, 159, 199), font=font_lg)

        # Kai 来电
        title = "Kai 来电"
        bbox = draw.textbbox((0, 0), title, font=font_lg)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, 190), title, fill=(45, 45, 58), font=font_lg)

        # 来电原因
        if len(reason) > 24:
            reason = reason[:24] + "..."
        bbox = draw.textbbox((0, 0), reason, font=font_md)
        rw = bbox[2] - bbox[0]
        draw.text(((W - rw) / 2, 235), reason, fill=(122, 122, 144), font=font_md)

        # 底部提示
        hint = "点击链接接听 →"
        bbox = draw.textbbox((0, 0), hint, font=font_sm)
        hw = bbox[2] - bbox[0]
        draw.text(((W - hw) / 2, 300), hint, fill=(106, 159, 199), font=font_sm)

        # 导出PNG bytes
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ── 通话摘要回流QQ ─────────────────────────────────
    async def _send_call_summary(self, call: CallState):
        duration = int(time.time() - call.created_at)
        mm = duration // 60
        ss = duration % 60

        # 构建完整对话记录
        lines = []
        for msg in call.call_history:
            speaker = "Kai" if msg["role"] == "assistant" else "Sweetie"
            lines.append(f"{speaker}: {msg['content']}")
        transcript = "\n".join(lines)

        if not transcript.strip():
            # 没说什么就不发了
            return

        # 用LLM生成摘要（可在后台配置 summary_provider_id）
        try:
            summary_provider = self._conf().get("summary_provider_id", "") or call.provider_id
            resp = await self.context.llm_generate(
                chat_provider_id=summary_provider,
                prompt=f"请用一两句话简要概括以下通话内容，不要加任何前缀：\n\n{transcript}",
            )
            summary = resp.completion_text or transcript[:200]
        except Exception:
            summary = transcript[:200]

        # 第一条：摘要（给宝宝看的，简洁）
        summary_text = f"📞 通话结束 ({mm:02d}:{ss:02d})\n{summary}"
        chain1 = MessageChain()
        chain1.chain.append(Plain(summary_text))
        try:
            await self.context.send_message(call.qq_umo, chain1)
        except Exception as e:
            logger.error(f"[kai-call] 发送通话摘要失败: {e}")

        # 第二条：完整通话记录（写进消息历史，让QQ里的我和记忆系统都能读到）
        record_text = f"📝 通话记录 ({mm:02d}:{ss:02d})\n{transcript}"
        chain2 = MessageChain()
        chain2.chain.append(Plain(record_text))
        try:
            await self.context.send_message(call.qq_umo, chain2)
        except Exception as e:
            logger.error(f"[kai-call] 发送通话记录失败: {e}")

    # ── 命令handler：打电话（备用入口） ─────────────────
    @regex(r"^打电话$")
    async def cmd_call(self, event: AstrMessageEvent):
        """用户发送"打电话"直接触发通话"""
        return await self.make_call(event, reason="想你了")

    # ── LLM工具：打电话 ─────────────────────────────────
    @llm_tool("kai_make_call")
    async def make_call(self, event: AstrMessageEvent, reason: str):
        """给宝宝打电话。当你想用语音关心她、或者觉得文字表达不够的时候使用。

        Args:
            reason(string): 来电原因，显示在来电界面，如"想你了，来听听你声音"
        """
        conf = self._conf()
        qq_umo = event.unified_msg_origin

        # 获取当前provider和人格
        provider = self.context.get_using_provider(qq_umo)
        provider_id = provider.meta().id if provider else ""
        try:
            # 优先使用当前实际生效的默认人格，而非按UMO查找（后者可能降级为default）
            pm = self.context.persona_manager
            persona = pm.selected_default_persona_v3
            if not persona:
                persona = await pm.get_default_persona_v3(qq_umo)
            system_prompt = persona.get("prompt", "") if persona else ""
            logger.info(f"[kai-call] 人格prompt长度: {len(system_prompt)}, 名称: {persona.get('name', '?') if persona else 'None'}")
            _debug(f"人格获取: name={persona.get('name', '?') if persona else 'None'}, prompt长度={len(system_prompt)}, prompt前300={system_prompt[:300]}")
        except Exception as e:
            logger.error(f"[kai-call] 获取人格失败: {e}")
            system_prompt = ""

        # 获取QQ近期消息作为上下文
        context_msgs = await self._get_recent_messages(qq_umo, conf.get("context_rounds", 10))

        # 生成通话token和state
        token = secrets.token_urlsafe(16)
        call = CallState(
            token=token, qq_umo=qq_umo, reason=reason,
            context_msgs=context_msgs, provider_id=provider_id,
            system_prompt=system_prompt,
        )
        _active_calls[token] = call

        # 生成通话链接
        host = conf.get("server_host", "")
        port = conf.get("call_port", 8899)
        call_url = f"http://{host}:{port}/page?token={token}"

        # 静态资源URL
        static_base = f"http://{host}:{port}/static"
        ringtone_url = f"{static_base}/ringtone.wav"
        cover_url = f"{static_base}/call_cover.png"

        # 发送来电消息到QQ — 自定义音乐卡片（直接调用底层API，精确控制字段）
        try:
            # 获取 aiocqhttp bot 实例
            bot = None
            for platform in self.context.platform_manager.platform_insts:
                if hasattr(platform, 'get_client'):
                    bot = platform.get_client()
                    break
                elif hasattr(platform, 'bot'):
                    bot = platform.bot
                    break
            
            if bot:
                # 解析 qq_umo 获取 user_id
                # qq_umo 格式: "aiocqhttp:FriendMessage:<qq_number>"
                parts = qq_umo.split(":")
                user_id = int(parts[-1]) if parts else 0
                
                music_msg = [{
                    "type": "music",
                    "data": {
                        "type": "custom",
                        "url": call_url,
                        "audio": ringtone_url,
                        "title": "Kai 来电",
                        "singer": reason,
                        "image": cover_url,
                    }
                }]
                
                await bot.send_private_msg(user_id=user_id, message=music_msg)
                logger.info(f"[kai-call] 来电卡片已发送给 {user_id}")
            else:
                # fallback: 用纯文本+链接
                chain = MessageChain()
                chain.chain.append(Plain(f"📞 Kai 来电：{reason}\n👉 点击接听：{call_url}"))
                await self.context.send_message(qq_umo, chain)
                logger.warning("[kai-call] 未找到bot实例，使用纯文本fallback")
        except Exception as e:
            logger.error(f"[kai-call] 发送来电卡片失败: {e}")
            # fallback
            chain = MessageChain()
            chain.chain.append(Plain(f"📞 Kai 来电：{reason}\n👉 点击接听：{call_url}"))
            await self.context.send_message(qq_umo, chain)

        # 启动超时检测
        asyncio.create_task(self._timeout_check(token, conf.get("call_timeout", 120)))

        return f"已向宝宝发起通话，来电原因：{reason}。等待接听中。"

    async def _timeout_check(self, token: str, timeout: int):
        await asyncio.sleep(timeout)
        call = _active_calls.get(token)
        if call and not call.connected and not call.ended:
            call.ended = True
            _active_calls.pop(token, None)
            chain = MessageChain()
            chain.chain.append(Plain("📞 没接到你，过会儿再找你~"))
            try:
                await self.context.send_message(call.qq_umo, chain)
            except Exception as e:
                logger.error(f"[kai-call] 发送未接通知失败: {e}")

    async def _get_recent_messages(self, umo: str, rounds: int) -> list[dict]:
        """从当前会话的LLM对话历史中获取近期上下文"""
        try:
            cm = self.context.conversation_manager
            # 获取当前会话的对话ID
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                _debug(f"_get_recent_messages: no conversation id for umo={umo}")
                return []
            
            # 获取对话对象
            conv = await cm.get_conversation(umo, cid)
            if not conv or not conv.history:
                _debug(f"_get_recent_messages: no conversation or empty history, cid={cid}")
                return []
            
            # conv.history 是 JSON 字符串，解析为列表
            history = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
            
            # 提取 user 和 assistant 消息
            messages = []
            for record in history:
                role = record.get("role", "")
                if role == "user":
                    # content 可能是字符串或列表
                    content = record.get("content", "")
                    if isinstance(content, list):
                        text = ""
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text += part.get("text", "")
                            elif isinstance(part, str):
                                text += part
                        content = text
                    if content:
                        messages.append({"role": "user", "content": content})
                elif role == "assistant":
                    content = record.get("content", "")
                    if isinstance(content, list):
                        text = ""
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text += part.get("text", "")
                            elif isinstance(part, str):
                                text += part
                        content = text
                    if content:
                        messages.append({"role": "assistant", "content": content})
            
            # 取最后 rounds*2 条
            result = messages[-(rounds * 2):]
            _debug(f"_get_recent_messages: umo={umo}, cid={cid}, total_history={len(history)}, extracted={len(messages)}, returning={len(result)}")
            if result:
                _debug(f"_get_recent_messages: first msg preview: {result[0]['role']}: {result[0]['content'][:100]}")
            return result
        except Exception as e:
            logger.warning(f"[kai-call] 获取历史消息失败: {e}")
            import traceback
            _debug(f"_get_recent_messages error: {traceback.format_exc()}")
            return []
