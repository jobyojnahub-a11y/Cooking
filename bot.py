import os
import json
import requests
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
import subprocess
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from telegram.error import TelegramError

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# States
ADMIN_MENU, ENTER_BATCH_ID, ENTER_TOKEN, MANAGE_BATCH, CONFIRM_TOKEN = range(5)

# Config file
CONFIG_FILE = "bot_config.json"

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
RENDER_URL = os.getenv("RENDER_URL", "")


class BotConfig:
    def __init__(self):
        self.data = self.load_config()

    def load_config(self) -> Dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "batches": {},
            "channels": {},
            "processed_lectures": []
        }

    def save(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"Config save error: {e}")

    def add_batch(self, batch_id: str, token: str, channel_id: int, batch_name: str):
        self.data["batches"][batch_id] = {
            "token": token,
            "channel_id": channel_id,
            "name": batch_name,
            "connected_at": datetime.now().isoformat(),
            "last_check": None,
            "active": True
        }
        self.save()

    def get_batch(self, batch_id: str) -> Optional[Dict]:
        return self.data["batches"].get(batch_id)

    def update_token(self, batch_id: str, token: str):
        if batch_id in self.data["batches"]:
            self.data["batches"][batch_id]["token"] = token
            self.save()

    def get_all_batches(self) -> Dict:
        return self.data.get("batches", {})

    def mark_lecture_processed(self, lecture_id: str):
        if lecture_id not in self.data["processed_lectures"]:
            self.data["processed_lectures"].append(lecture_id)
            if len(self.data["processed_lectures"]) > 10000:
                self.data["processed_lectures"] = self.data["processed_lectures"][-5000:]
            self.save()

    def is_lecture_processed(self, lecture_id: str) -> bool:
        return lecture_id in self.data["processed_lectures"]


config = BotConfig()


class APIHandler:
    BASE_URL = "https://api.penpencil.co"

    @staticmethod
    def get_batch_details(batch_id: str, token: str) -> Optional[Dict]:
        try:
            headers = {
                "accept": "application/json",
                "authorization": f"Bearer {token}",
                "client-id": "5eb393ee95fab7468a79d189",
                "client-type": "WEB"
            }
            url = f"{APIHandler.BASE_URL}/v3/batches/{batch_id}/details?type=EXPLORE_LEAD"
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Batch details error: {e}")
            return None

    @staticmethod
    def get_todays_schedule(batch_id: str, token: str) -> Optional[Dict]:
        try:
            headers = {
                "authorization": f"Bearer {token}",
                "client-type": "WEB",
                "content-type": "application/json"
            }
            url = f"{APIHandler.BASE_URL}/v1/batches/{batch_id}/todays-schedule"
            params = {
                "batchId": batch_id,
                "isNewStudyMaterialFlow": "true"
            }
            response = requests.get(url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Todays schedule error: {e}")
            return None

    @staticmethod
    def get_video_url(child_id: str, parent_id: str, token: str) -> Optional[str]:
        try:
            headers = {
                "authorization": f"Bearer {token}",
                "client-type": "WEB",
                "client-version": "201"
            }
            url = f"{APIHandler.BASE_URL}/v1/videos/video-url-details"
            params = {
                "type": "BATCHES",
                "videoContainerType": "DASH",
                "reqType": "query",
                "childId": child_id,
                "parentId": parent_id,
                "clientVersion": "201"
            }
            response = requests.get(url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json().get("data", {})
            if data.get("url"):
                return data["url"] + data.get("signedUrl", "")
            return None
        except Exception as e:
            logger.error(f"Video URL error: {e}")
            return None

    @staticmethod
    def generate_m3u8(mpd_url: str) -> Optional[str]:
        try:
            response = requests.post(
                "https://play2.bhanuyadav.workers.dev/generate",
                json={"url": mpd_url},
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data.get("data", {}).get("url")
            return None
        except Exception as e:
            logger.error(f"M3U8 generation error: {e}")
            return None


class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.upload_tasks = {}
        self.setup_handlers()

    def setup_handlers(self):
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("admin", self.admin_menu)],
            states={
                ADMIN_MENU: [CallbackQueryHandler(self.admin_choice)],
                ENTER_BATCH_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_batch_id)],
                ENTER_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_token)],
                CONFIRM_TOKEN: [CallbackQueryHandler(self.confirm_token)],
                MANAGE_BATCH: [CallbackQueryHandler(self.manage_batch)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
        )

        self.app.add_handler(conv_handler)
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("listbatches", self.list_batches))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("updatetoken", self.update_token_cmd))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🎓 PenPencil Lecture Bot\n\n"
            "📚 Daily lecture upload bot\n"
            "👨‍💼 Use /admin to manage batches\n"
            "📋 Use /listbatches to see batches\n"
            "❓ Use /help for info",
            parse_mode="Markdown"
        )

    async def admin_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.from_user.id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Unauthorized access")
            return ConversationHandler.END

        keyboard = [
            [InlineKeyboardButton("➕ Connect New Batch", callback_data="connect")],
            [InlineKeyboardButton("📋 My Batches", callback_data="mybatches")],
            [InlineKeyboardButton("🔑 Update Token", callback_data="updatetoken")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "**⚙️ Admin Panel**\n\nChoose an action:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return ADMIN_MENU

    async def admin_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "connect":
            await query.edit_message_text(
                text="📤 **Send Batch ID**\n\nExample: `68df734c7363e718cad6cd55`",
                parse_mode="Markdown"
            )
            return ENTER_BATCH_ID

        elif query.data == "mybatches":
            batches = config.get_all_batches()
            if not batches:
                await query.edit_message_text("No batches connected yet")
                return ADMIN_MENU

            text = "**📚 Connected Batches:**\n\n"
            keyboard = []
            for bid, data in batches.items():
                status = "🟢 Active" if data.get("active") else "🔴 Inactive"
                text += f"📌 {data['name']}\nID: `{bid}`\n{status}\n\n"
                keyboard.append(
                    [InlineKeyboardButton(f"Edit {data['name'][:20]}", callback_data=f"edit_{bid}")]
                )

            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return MANAGE_BATCH

        elif query.data == "updatetoken":
            batches = config.get_all_batches()
            if not batches:
                await query.edit_message_text("No batches to update")
                return ADMIN_MENU

            keyboard = []
            for bid, data in batches.items():
                keyboard.append([InlineKeyboardButton(data['name'], callback_data=f"seltoken_{bid}")])
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])

            await query.edit_message_text(
                "**Select batch to update token:**",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            context.user_data["action"] = "updatetoken"
            return ENTER_TOKEN

        elif query.data == "back":
            await query.delete_message()
            return ConversationHandler.END

        elif query.data == "cancel":
            await query.delete_message()
            return ConversationHandler.END

    async def process_batch_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        batch_id = update.message.text.strip()
        context.user_data["batch_id"] = batch_id

        await update.message.reply_text(
            "🔐 **Send Bearer Token**\n\nPaste your JWT bearer token:",
            parse_mode="Markdown"
        )
        return ENTER_TOKEN

    async def process_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        token = update.message.text.strip()
        batch_id = context.user_data.get("batch_id")

        await update.message.reply_text("⏳ Verifying token and batch...")

        details = APIHandler.get_batch_details(batch_id, token)
        if not details or not details.get("success"):
            await update.message.reply_text("❌ Failed to connect. Invalid token or batch ID.")
            return ADMIN_MENU

        batch_data = details.get("data", {})
        batch_name = batch_data.get("name", "Unknown")

        context.user_data["token"] = token
        context.user_data["batch_name"] = batch_name

        keyboard = [
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm_yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="confirm_no")],
        ]

        await update.message.reply_text(
            f"**Confirm Connection:**\n\n"
            f"📚 Batch: {batch_name}\n"
            f"🆔 ID: `{batch_id}`\n"
            f"📍 Channel: {update.message.chat_id}\n\n"
            f"Lectures will upload daily.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CONFIRM_TOKEN

    async def confirm_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "confirm_yes":
            batch_id = context.user_data.get("batch_id")
            token = context.user_data.get("token")
            batch_name = context.user_data.get("batch_name")
            chat_id = query.message.chat_id

            config.add_batch(batch_id, token, chat_id, batch_name)

            await query.edit_message_text(
                f"✅ **Connected Successfully!**\n\n"
                f"📚 {batch_name}\n"
                f"🚀 Lectures will upload daily\n"
                f"⏰ Check interval: Every 10 minutes",
                parse_mode="Markdown"
            )

            if batch_id not in self.upload_tasks:
                task = asyncio.create_task(self.lecture_upload_task(batch_id, chat_id, token))
                self.upload_tasks[batch_id] = task

            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Cancelled")
            return ConversationHandler.END

    async def manage_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data.startswith("edit_"):
            batch_id = query.data.replace("edit_", "")
            batch = config.get_batch(batch_id)

            keyboard = [
                [InlineKeyboardButton("🔑 Update Token", callback_data=f"token_{batch_id}")],
                [InlineKeyboardButton("🗑 Remove Batch", callback_data=f"remove_{batch_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ]

            await query.edit_message_text(
                f"**{batch['name']}**\n\nConnected: {batch['connected_at'][:10]}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

        elif query.data.startswith("remove_"):
            batch_id = query.data.replace("remove_", "")
            if batch_id in config.data["batches"]:
                del config.data["batches"][batch_id]
                config.save()
                if batch_id in self.upload_tasks:
                    self.upload_tasks[batch_id].cancel()
                    del self.upload_tasks[batch_id]
            await query.edit_message_text("✅ Batch removed")
            return ADMIN_MENU

        elif query.data == "back":
            await query.delete_message()
            return ADMIN_MENU

    async def update_token_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.from_user.id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Unauthorized")
            return

        batches = config.get_all_batches()
        if not batches:
            await update.message.reply_text("No batches connected")
            return

        keyboard = []
        for bid, data in batches.items():
            keyboard.append([InlineKeyboardButton(data['name'], callback_data=f"upd_{bid}")])

        await update.message.reply_text(
            "**Select batch to update token:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    async def list_batches(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        batches = config.get_all_batches()
        if not batches:
            await update.message.reply_text("📭 No batches connected")
            return

        text = "**📚 Connected Batches:**\n\n"
        for bid, data in batches.items():
            status = "🟢 Active" if data.get("active") else "🔴 Inactive"
            text += f"• {data['name']}\n  Status: {status}\n"

        await update.message.reply_text(text, parse_mode="Markdown")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "**Commands:**\n\n"
            "/admin - 👨‍💼 Admin panel\n"
            "/listbatches - 📋 Show batches\n"
            "/updatetoken - 🔑 Update token\n"
            "/help - ❓ Help\n"
            "/cancel - ❌ Cancel\n\n"
            "**Features:**\n\n"
            "✅ Multi-batch support\n"
            "✅ Auto daily uploads\n"
            "✅ PDF & Video support\n"
            "✅ Progress tracking\n"
            "✅ Token management\n\n"
            "**Contact:** @YourContact\n"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Operation cancelled.")
        return ConversationHandler.END

    async def lecture_upload_task(self, batch_id: str, channel_id: int, token: str):
        logger.info(f"Started upload task for batch {batch_id}")

        await asyncio.sleep(30)

        while True:
            try:
                batch = config.get_batch(batch_id)
                if not batch or not batch.get("active"):
                    logger.info(f"Batch {batch_id} inactive, stopping task")
                    break

                schedule = APIHandler.get_todays_schedule(batch_id, token)
                if not schedule or not schedule.get("success"):
                    logger.warning(f"Failed to fetch schedule for {batch_id}")
                    await asyncio.sleep(600)
                    continue

                lectures = schedule.get("data", [])
                logger.info(f"Found {len(lectures)} lectures for {batch_id}")

                for lecture in lectures:
                    lecture_id = lecture.get("_id")

                    if config.is_lecture_processed(lecture_id):
                        continue

                    try:
                        await self.process_lecture(lecture, batch_id, channel_id, token)
                        config.mark_lecture_processed(lecture_id)
                    except Exception as e:
                        logger.error(f"Error processing lecture {lecture_id}: {e}")

                await asyncio.sleep(600)

            except asyncio.CancelledError:
                logger.info(f"Upload task for {batch_id} cancelled")
                break
            except Exception as e:
                logger.error(f"Error in upload task for {batch_id}: {e}")
                await asyncio.sleep(600)

    async def process_lecture(self, lecture: Dict, batch_id: str, channel_id: int, token: str):
        title = lecture.get("topic", "Lecture")

        homeworks = lecture.get("homeworkIds", [])
        for hw in homeworks:
            hw_title = hw.get("topic", "Document")
            for attachment in hw.get("attachmentIds", []):
                try:
                    base_url = attachment.get("baseUrl", "")
                    key = attachment.get("key", "")
                    name = attachment.get("name", "file.pdf")

                    if base_url and key:
                        pdf_url = base_url + key
                    elif base_url and name:
                        pdf_url = base_url + name
                    else:
                        continue

                    await self.send_pdf(channel_id, pdf_url, hw_title)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"PDF error: {e}")

        if lecture.get("_id") and lecture.get("urlType") != "vimeo":
            try:
                video_url = await self.get_processed_video_url(
                    lecture.get("_id"), batch_id, token
                )
                if video_url:
                    await self.download_and_send_video(channel_id, video_url, title)
            except Exception as e:
                logger.error(f"Video error: {e}")

    async def get_processed_video_url(self, lecture_id: str, batch_id: str, token: str) -> Optional[str]:
        mpd_url = APIHandler.get_video_url(lecture_id, batch_id, token)
        if not mpd_url:
            return None

        m3u8_url = APIHandler.generate_m3u8(mpd_url)
        return m3u8_url

    async def send_pdf(self, channel_id: int, pdf_url: str, title: str):
        try:
            await self.app.bot.send_document(
                chat_id=channel_id,
                document=pdf_url,
                caption=f"📄 {title[:100]}"
            )
            logger.info(f"Sent PDF: {title}")
        except TelegramError as e:
            logger.error(f"Telegram error sending PDF: {e}")
        except Exception as e:
            logger.error(f"Error sending PDF: {e}")

    async def download_and_send_video(self, channel_id: int, m3u8_url: str, title: str):
        output_file = None
        progress_msg = None

        try:
            output_file = f"/tmp/lecture_{int(time.time())}.mp4"

            progress_msg = await self.app.bot.send_message(
                channel_id,
                f"⏳ Downloading video...\n📺 {title[:50]}"
            )

            cmd = [
                "yt-dlp",
                "-f", "best",
                "-o", output_file,
                "--quiet",
                m3u8_url
            ]

            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            try:
                stdout, stderr = process.communicate(timeout=300)
                if process.returncode != 0:
                    logger.error(f"yt-dlp error: {stderr.decode()}")
                    return
            except subprocess.TimeoutExpired:
                process.kill()
                logger.error("Download timeout")
                return

            if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
                logger.error("Downloaded file is empty or missing")
                return

            await progress_msg.edit_text(f"📤 Uploading to Telegram...\n📺 {title[:50]}")

            with open(output_file, 'rb') as video:
                await self.app.bot.send_video(
                    chat_id=channel_id,
                    video=video,
                    caption=f"🎬 {title[:200]}",
                    timeout=300
                )

            logger.info(f"Sent video: {title}")
            await progress_msg.delete()

        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            if progress_msg:
                try:
                    await progress_msg.delete()
                except:
                    pass
        except Exception as e:
            logger.error(f"Video upload error: {e}")
            if progress_msg:
                try:
                    await progress_msg.delete()
                except:
                    pass
        finally:
            if output_file and os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except:
                    pass

    def run(self):
        logger.info("Starting bot...")

        for batch_id, batch_data in config.get_all_batches().items():
            if batch_data.get("active"):
                task = asyncio.create_task(
                    self.lecture_upload_task(
                        batch_id,
                        batch_data["channel_id"],
                        batch_data["token"]
                    )
                )
                self.upload_tasks[batch_id] = task

        self.app.run_polling()


def main():
    if not ADMIN_USER_ID or ADMIN_USER_ID == 0:
        logger.error("ADMIN_USER_ID not set!")
        return

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return

    logger.info(f"Admin User ID: {ADMIN_USER_ID}")
    logger.info(f"Bot Token set: {bool(BOT_TOKEN)}")

    bot = TelegramBot(BOT_TOKEN)
    bot.run()


if __name__ == "__main__":
    main()
