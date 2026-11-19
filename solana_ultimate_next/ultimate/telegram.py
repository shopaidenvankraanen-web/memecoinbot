"""Private-chat command adapter and replay of trusted local Telegram fixtures."""
import re

from .market import mapping

COMMAND = re.compile(r"/([a-z0-9_]{1,32})")


class Telegram:
    def __init__(self, commands):
        self.commands = commands

    async def handle(self, user, chat, chat_type, message_id, name, args, *, replay=False):
        if (chat_type != "private" or type(user) is not int or type(chat) is not int
                or chat != user or not 0 < user < 2**52):
            return None
        if user not in self.commands.allowed:
            return "Geen toegang."
        if (type(message_id) is not int or not 0 < message_id < 2**53
                or not isinstance(args, (tuple, list)) or len(args) > 32
                or any(not isinstance(arg, str) or len(arg) > 512 for arg in args)):
            return "Ongeldige invoer."
        # Use separate watermarks because fixtures can reuse real message IDs.
        namespace = "telegram_replay" if replay else "telegram"
        key = f"{namespace}:{chat}"
        with self.commands.store.transaction(write=True) as db:
            claimed = db.execute("""
                INSERT INTO settings VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE excluded.value > settings.value
                """, (key, message_id)).rowcount
        if not claimed:
            return None
        if replay and name == "scheduler_start":
            return "Gebruik ingest voor een eindige offline scanreeks."
        return await self.commands.dispatch(user, True, name, args)

    async def replay(self, update):
        message = mapping(mapping(update).get("message"))
        user = mapping(message.get("from"))
        chat = mapping(message.get("chat"))
        text = message.get("text")
        if (user.get("is_bot") is not False or not isinstance(text, str)
                or not text.startswith("/") or len(text) > 4096):
            return None
        command, *args = text.split()
        match = COMMAND.fullmatch(command)
        if not match:
            return None
        return await self.handle(user.get("id"), chat.get("id"), chat.get("type"),
                                 message.get("message_id"), match[1], args, replay=True)

    def register(self, application, handler_factory, *, silent_unauthorized=False):
        for name in self.commands.registry:
            async def callback(update, context, command=name):
                user, chat, message = update.effective_user, update.effective_chat, update.message
                if message is None or chat is None or user is None or user.is_bot:
                    return
                if silent_unauthorized and user.id not in self.commands.allowed:
                    return
                response = await self.handle(user.id, chat.id, chat.type, message.message_id,
                                             command, context.args or ())
                if response is not None:
                    await message.reply_text(response, parse_mode=None, disable_web_page_preview=True)

            application.add_handler(handler_factory(name, callback))
