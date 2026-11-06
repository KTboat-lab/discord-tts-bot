import asyncio
import io
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
import imageio_ffmpeg
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from gtts import gTTS

# ป้องกันปัญหา UnicodeEncodeError บน Windows PowerShell (cp1252)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO)

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT_SECONDS", "900"))  # 15 นาที = 900 วินาที
FFMPEG_EXECUTABLE = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

if not discord.opus.is_loaded():
    if not discord.opus._load_default():
        for opus_lib in ("libopus.so.0", "libopus.so", "opus"):
            try:
                discord.opus.load_opus(opus_lib)
                break
            except Exception:
                pass


def synthesize_tts_bytes(text: str) -> bytes:
    """
    แปลงข้อความเป็นไฟล์เสียง MP3 ในหน่วยความจำ (Bytes)
    ใช้เสียง Google Translate TTS ภาษาไทย (lang='th') ซึ่งเป็นเสียงเดียวกับ TipMe
    สามารถอ่านได้ทั้งภาษาไทยและภาษาอังกฤษในประโยคเดียวกันด้วยโทนเสียงโดเนทที่คุ้นเคย
    """
    tts = gTTS(text=text, lang="th", slow=False)
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    fp.seek(0)
    return fp.read()


@dataclass
class GuildTTSState:
    owner_id: Optional[int] = None
    channel_id: Optional[int] = None
    last_active: float = 0.0
    idle_task: Optional[asyncio.Task] = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    player_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_state(self):
        current = asyncio.current_task()
        self.owner_id = None
        self.channel_id = None
        self.last_active = 0.0
        if self.idle_task and not self.idle_task.done() and self.idle_task is not current:
            self.idle_task.cancel()
        self.idle_task = None
        if self.player_task and not self.player_task.done() and self.player_task is not current:
            self.player_task.cancel()
        self.player_task = None
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break


class TTSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)
        self.guild_states: dict[int, GuildTTSState] = {}

    def get_state(self, guild_id: int) -> GuildTTSState:
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildTTSState()
        return self.guild_states[guild_id]

    async def setup_hook(self):
        await self.tree.sync()
        # เปิด HTTP Health Check Server สำหรับ Render.com (Free Web Service)
        port = os.getenv("PORT")
        if port:
            app = web.Application()
            app.router.add_get("/", lambda request: web.Response(text="Mogged-bot TTS is running!"))
            app.router.add_get("/health", lambda request: web.Response(text="OK"))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(port))
            await site.start()
            print(f"🌐 เปิด HTTP Health Check บนพอร์ต {port} สำหรับ Render.com แล้ว", flush=True)

            # ระบบ Self-Ping ทุก 10 นาที เพื่อป้องกันไม่ให้ Render Free Tier หลับ
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                asyncio.create_task(self._keep_alive_pinger(render_url))

    async def _keep_alive_pinger(self, base_url: str):
        ping_url = f"{base_url.rstrip('/')}/health"
        await asyncio.sleep(60)
        async with aiohttp.ClientSession() as session:
            while not self.is_closed():
                try:
                    async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        print(f"[KeepAlive] Pinged {ping_url} -> {resp.status}", flush=True)
                except Exception as e:
                    print(f"[KeepAlive Error] {e}", flush=True)
                await asyncio.sleep(600)  # ทุกๆ 10 นาที (ก่อนครบ 15 นาทีของ Render)


bot = TTSBot()


async def wait_for_voice_ready(vc: discord.VoiceClient, timeout: float = 5.0):
    """
    รอให้การเชื่อมต่อเสียงและโปรโตคอลเข้ารหัส DAVE (E2EE) ของ Discord พร้อมส่งเสียงจริง
    ป้องกันปัญหาเข้าห้องปุ๊บพูดปั๊บแล้วคำแรกเสียงไม่ออก
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if vc.is_connected():
            conn = getattr(vc, "_connection", None)
            if conn is None:
                break
            dave_ver = getattr(conn, "dave_protocol_version", 0)
            can_enc = getattr(conn, "can_encrypt", False)
            if dave_ver == 0 or can_enc:
                print(f"[Voice Ready] dave_version={dave_ver} can_encrypt={can_enc} elapsed={time.monotonic()-start:.2f}s", flush=True)
                break
        await asyncio.sleep(0.1)
    else:
        conn = getattr(vc, "_connection", None)
        print(f"[Voice Ready Timeout] dave_version={getattr(conn, 'dave_protocol_version', None)} can_encrypt={getattr(conn, 'can_encrypt', None)}", flush=True)
    await asyncio.sleep(0.3)


async def start_or_reset_idle_timer(guild: discord.Guild, state: GuildTTSState):
    """ตั้งเวลา (หรือรีเซ็ตเวลา) Idle 15 นาที เมื่อครบกำหนดจะออกจากห้องเอง"""
    state.last_active = time.monotonic()
    if state.idle_task and not state.idle_task.done():
        state.idle_task.cancel()

    async def _idle_watchdog():
        try:
            while True:
                elapsed = time.monotonic() - state.last_active
                remaining = IDLE_TIMEOUT - elapsed
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)

            vc = guild.voice_client
            if vc and vc.is_playing():
                while vc.is_playing():
                    await asyncio.sleep(1)

            state.reset_state()
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        except asyncio.CancelledError:
            pass

    state.idle_task = asyncio.create_task(_idle_watchdog())


async def audio_player_worker(guild: discord.Guild, state: GuildTTSState):
    """ดึงข้อความจากคิวมาแปลงเป็นเสียงและพูดทีละประโยค"""
    try:
        while True:
            text = await state.queue.get()
            tmp_path = None
            try:
                vc = guild.voice_client
                if not vc or not vc.is_connected():
                    continue

                await wait_for_voice_ready(vc)

                audio_bytes = await asyncio.to_thread(synthesize_tts_bytes, text)

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
                    tmp_file.write(audio_bytes)
                    tmp_path = tmp_file.name

                audio_source = discord.FFmpegPCMAudio(
                    tmp_path,
                    executable=FFMPEG_EXECUTABLE,
                    options="-filter:a volume=1.5",
                )

                done_event = asyncio.Event()

                def _after_play(error):
                    if error:
                        print(f"[Voice Error] {error}", flush=True)
                    bot.loop.call_soon_threadsafe(done_event.set)

                vc.play(audio_source, after=_after_play)
                await done_event.wait()
                # อัปเดตเวลาล่าสุดหลังพูดจบ เพื่อให้เริ่มนับ Idle 15 นาทีหลังพูดจบประโยคล่าสุด
                await start_or_reset_idle_timer(guild, state)
            except Exception as e:
                print(f"[Player Exception] {e}", flush=True)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                state.queue.task_done()
    except asyncio.CancelledError:
        pass


async def handle_tts_request(
    guild: discord.Guild,
    user: discord.Member,
    text: str,
) -> tuple[bool, str]:
    """
    ตรวจสอบสิทธิ์แบบ 1:1 และจัดการเข้าห้อง/พูดข้อความ
    คืนค่า (สำเร็จหรือไม่, ข้อความตอบกลับ)
    """
    text = text.strip()
    if not text:
        return False, "❌ กรุณาพิมพ์ข้อความที่ต้องการให้บอทพูด"

    if len(text) > 500:
        return False, "❌ ข้อความยาวเกินไป (สูงสุด 500 ตัวอักษร)"

    state = bot.get_state(guild.id)

    async with state.lock:
        vc = guild.voice_client

        # กรณีบอทหลุดจากห้องไปแล้วแต่สถานะยังค้างอยู่ ให้ล้างสถานะก่อน
        if vc is None or not vc.is_connected():
            state.reset_state()
            vc = None

        # ตรวจสอบว่ามีคนอื่นถือสิทธิ์ 1:1 อยู่หรือไม่
        if state.owner_id is not None and state.owner_id != user.id:
            elapsed = time.monotonic() - state.last_active
            remaining_sec = max(0, int(IDLE_TIMEOUT - elapsed))
            mins, secs = divmod(remaining_sec, 60)
            return (
                False,
                f"🔒 ขณะนี้บอทกำลังใช้งานแบบ 1:1 อยู่กับ <@{state.owner_id}> "
                f"(จะว่างเมื่อบอทถูกเตะออกจากห้อง หรือไม่มีการใช้งานครบ 15 นาที — เหลือเวลาประมาณ {mins} นาที {secs} วินาที)",
            )

        # ผู้เรียกต้องอยู่ในห้องเสียง
        if not user.voice or not user.voice.channel:
            return False, "❌ คุณต้องอยู่ในห้องเสียง (Voice Channel) ก่อนเรียกใช้งานบอท"

        target_channel = user.voice.channel

        # กรณีเริ่มต้นใช้งานใหม่ (ยังไม่มีเจ้าของ)
        if state.owner_id is None:
            try:
                if vc is None:
                    vc = await target_channel.connect(self_deaf=False, self_mute=False)
                elif vc.channel.id != target_channel.id:
                    await vc.move_to(target_channel)
            except Exception as e:
                return False, f"❌ ไม่สามารถเข้าห้องเสียงได้: {e}"

            state.owner_id = user.id
            state.channel_id = target_channel.id
        else:
            # กรณีเจ้าของเดิมสั่งซ้ำ: หากเจ้าของย้ายห้อง บอทจะตามไปหาเจ้าของคนเดิม
            if vc.channel.id != target_channel.id:
                try:
                    await vc.move_to(target_channel)
                    state.channel_id = target_channel.id
                except Exception as e:
                    return False, f"❌ ไม่สามารถย้ายตามไปยังห้องเสียงได้: {e}"

        # รีเซ็ตเวลา Idle 15 นาทีทุกครั้งที่เจ้าของสั่งงาน
        await start_or_reset_idle_timer(guild, state)

        # เริ่มตัวเล่นเสียงหากยังไม่ได้รัน
        if state.player_task is None or state.player_task.done():
            state.player_task = asyncio.create_task(audio_player_worker(guild, state))

        await state.queue.put(text)
        print(f"[TTS] Guild={guild.name} User={user} Text={text}", flush=True)
        return True, f"🔊 กำลังพูด: **{text}**"


@bot.event
async def on_ready():
    print(f"✅ บอทออนไลน์แล้วในชื่อ: {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"📌 Prefix: {COMMAND_PREFIX}say <ข้อความ> หรือ /say <ข้อความ>", flush=True)
    print(f"⏱️ ตั้งค่า Idle Timeout: {IDLE_TIMEOUT} วินาที ({IDLE_TIMEOUT / 60:.1f} นาที)", flush=True)
    print(f"🌐 เซิร์ฟเวอร์ที่บอทอยู่ตอนนี้: {[g.name for g in bot.guilds]}", flush=True)
    # ล้างสถานะค้างในห้องเสียงกรณีเพิ่งรีสตาร์ทบอทใหม่
    for guild in bot.guilds:
        if guild.me and guild.me.voice and guild.voice_client is None:
            try:
                await guild.change_voice_state(channel=None)
            except Exception:
                pass


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """ตรวจจับเมื่อบอทถูกเตะออกจากห้อง (Disconnect) เพื่อปลดล็อก 1:1 ทันที"""
    if bot.user and member.id == bot.user.id:
        if before.channel is not None and after.channel is None:
            state = bot.get_state(member.guild.id)
            state.reset_state()
            print(f"🔓 บอทถูกตัดการเชื่อมต่อจากห้องเสียงในเซิร์ฟเวอร์ {member.guild.name} — ปลดล็อกสถานะ 1:1 แล้ว", flush=True)


# ==================== คำสั่งแบบ Prefix (!say) ====================
@bot.command(name="say", aliases=["tts", "s"])
async def say_prefix(ctx: commands.Context, *, text: str = ""):
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return

    success, reply = await handle_tts_request(ctx.guild, ctx.author, text)
    await ctx.reply(reply, mention_author=False, delete_after=15 if not success else 10)


# ==================== คำสั่งแบบ Slash (/say) ====================
@bot.tree.command(name="say", description="ให้บอทพูดตามข้อความที่พิมพ์ (เสียงอ่านโดเนท TipMe แบบล็อก 1:1)")
@app_commands.describe(text="ข้อความที่ต้องการให้บอทพูด")
async def say_slash(interaction: discord.Interaction, text: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    success, reply = await handle_tts_request(interaction.guild, interaction.user, text)
    await interaction.followup.send(reply, ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "ใส่_TOKEN_ของบอทที่นี่":
        raise RuntimeError("กรุณาตั้งค่า DISCORD_TOKEN ในไฟล์ .env ก่อนรันบอท")
    bot.run(DISCORD_TOKEN)
