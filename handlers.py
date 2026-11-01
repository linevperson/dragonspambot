import logging
import asyncio
import secrets
from html import escape
from io import BytesIO
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
import qrcode

from database import db
from accounts import accounts
from spammer import flood_wait_remaining
from pyrogram.errors import SessionPasswordNeeded

logger = logging.getLogger(__name__)

router = Router()


# FSM States
class CampaignCreation(StatesGroup):
    waiting_for_name = State()


class MessageConfig(StatesGroup):
    waiting_for_message = State()


class GroupsManagement(StatesGroup):
    waiting_for_groups = State()
    waiting_for_group_selection = State()


class DelayConfig(StatesGroup):
    waiting_for_delay = State()


def format_delay_range(min_seconds: int, max_seconds: int) -> str:
    if min_seconds == max_seconds:
        if min_seconds >= 60 and min_seconds % 60 == 0:
            return f"{min_seconds // 60} мин"
        return f"{min_seconds} сек"
    if min_seconds >= 60 and min_seconds % 60 == 0 and max_seconds % 60 == 0:
        return f"{min_seconds // 60}–{max_seconds // 60} мин"
    return f"{min_seconds}–{max_seconds} сек"


class AccessManagement(StatesGroup):
    waiting_for_user_id = State()


class AccountLogin(StatesGroup):
    qr = State()
    password = State()


# ===== KEYBOARDS =====

def get_main_menu() -> InlineKeyboardMarkup:
    """Build main menu - list of campaigns."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➕ Создать кампанию", callback_data="create_campaign")
    )
    return builder.as_markup()


async def get_campaigns_list_keyboard(user_id: int, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Build keyboard with all campaigns."""
    connected = await accounts.is_connected(user_id)
    campaigns = await db.get_all_campaigns(user_id) if connected else []
    builder = InlineKeyboardBuilder()
    
    for campaign in campaigns:
        status = "🟢" if campaign['is_running'] else "⚪️"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {campaign['name']}",
                callback_data=f"campaign:{campaign['id']}"
            )
        )
    
    if connected:
        builder.row(
            InlineKeyboardButton(text="➕ Создать кампанию", callback_data="create_campaign")
        )
    builder.row(InlineKeyboardButton(text="🔑 Подключить аккаунт", callback_data="account_connect"))
    if is_owner:
        builder.row(
            InlineKeyboardButton(text="🔐 Доступ по ID", callback_data="access_menu")
        )
    
    return builder.as_markup()


def get_campaign_menu(campaign_id: int) -> InlineKeyboardMarkup:
    """Build campaign management menu."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📝 Добавить вариант поста",
            callback_data=f"add_message:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 Список постов",
            callback_data=f"list_messages:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="👥 Список групп",
            callback_data=f"groups_menu:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏱ Интервал для группы",
            callback_data=f"config_delay:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="▶️ СТАРТ",
            callback_data=f"start_campaign:{campaign_id}"
        ),
        InlineKeyboardButton(
            text="⏹ СТОП",
            callback_data=f"stop_campaign:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить кампанию",
            callback_data=f"delete_campaign:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(text="« Назад", callback_data="back_to_campaigns")
    )
    return builder.as_markup()


def get_groups_menu(campaign_id: int) -> InlineKeyboardMarkup:
    """Build groups management menu for a campaign."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📱 Выбрать из Telegram", callback_data=f"choose_groups:{campaign_id}")
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить группы",
            callback_data=f"add_groups:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 Показать все",
            callback_data=f"show_groups:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Очистить все",
            callback_data=f"clear_groups:{campaign_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="« Назад",
            callback_data=f"campaign:{campaign_id}"
        )
    )
    return builder.as_markup()


def get_back_to_campaign_button(campaign_id: int) -> InlineKeyboardMarkup:
    """Back to campaign button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="« К кампании",
            callback_data=f"campaign:{campaign_id}"
        )
    )
    return builder.as_markup()


def get_back_to_campaigns_button() -> InlineKeyboardMarkup:
    """Back to campaigns list button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="« К списку кампаний", callback_data="back_to_campaigns")
    )
    return builder.as_markup()


# ===== HANDLERS =====

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, is_owner: bool, user_id: int):
    """Start command - show campaigns list."""
    if await state.get_state() == GroupsManagement.waiting_for_group_selection.state:
        await state.clear()
        await message.answer("Выбор групп закрыт.", reply_markup=ReplyKeyboardRemove())
    if not await accounts.is_connected(user_id):
        async with accounts.lock(user_id):
            await accounts.cancel(user_id)
        keyboard = await get_campaigns_list_keyboard(user_id, is_owner)
        await state.clear()
        await message.answer(
            "Чтобы управлять кампаниями, подключите свой Telegram-аккаунт. "
            "Нажмите «Подключить аккаунт» и отсканируйте QR-код.",
            reply_markup=keyboard,
        )
        return
    campaigns = await db.get_all_campaigns(user_id)
    
    text = "🤖 <b>Spam Bot - Мульти-кампании</b>\n\n"
    if campaigns:
        text += f"📊 Активных кампаний: {len(campaigns)}\n\n"
        text += "Выберите кампанию для управления:"
    else:
        text += "Нет созданных кампаний.\nСоздайте первую кампанию!"
    
    keyboard = await get_campaigns_list_keyboard(user_id, is_owner)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "back_to_campaigns")
async def back_to_campaigns(callback: CallbackQuery, state: FSMContext, is_owner: bool, user_id: int):
    """Return to campaigns list."""
    selecting_groups = await state.get_state() == GroupsManagement.waiting_for_group_selection.state
    await state.clear()
    if selecting_groups:
        await callback.message.answer("Выбор групп закрыт.", reply_markup=ReplyKeyboardRemove())
    async with accounts.lock(user_id):
        await accounts.cancel(user_id)
    if not await accounts.is_connected(user_id):
        await callback.message.edit_text(
            "Чтобы управлять кампаниями, сначала подключите Telegram-аккаунт.",
            reply_markup=await get_campaigns_list_keyboard(user_id, is_owner),
        )
        await callback.answer()
        return
    campaigns = await db.get_all_campaigns(user_id)
    
    text = "🤖 <b>Spam Bot - Мульти-кампании</b>\n\n"
    if campaigns:
        text += f"📊 Активных кампаний: {len(campaigns)}\n\n"
        text += "Выберите кампанию для управления:"
    else:
        text += "Нет созданных кампаний.\nСоздайте первую кампанию!"
    
    keyboard = await get_campaigns_list_keyboard(user_id, is_owner)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def show_access_menu(callback: CallbackQuery):
    users = await db.get_authorized_users()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить ID", callback_data="access_add"))
    for user_id in users[:50]:
        builder.row(InlineKeyboardButton(
            text=f"🗑 Удалить {user_id}", callback_data=f"access_remove:{user_id}"
        ))
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="back_to_campaigns"))
    text = f"🔐 <b>Доступ к боту</b>\n\nГлавный администратор: <code>{callback.from_user.id}</code>\n"
    text += f"Допущено пользователей: {len(users)}"
    if len(users) > 50:
        text += "\nПоказаны первые 50 ID."
    await callback.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "account_connect")
async def account_connect(callback: CallbackQuery, state: FSMContext, user_id: int, owner_id: int):
    if callback.message.chat.type != "private":
        await callback.answer("Подключайте аккаунт в личном чате с ботом", show_alert=True)
        return
    await callback.answer()
    async with accounts.lock(user_id):
        if user_id != owner_id and not await db.is_authorized_user(user_id):
            return
        if user_id in accounts.clients:
            await callback.message.answer("Аккаунт уже подключён.")
            return
        await state.clear()
        try:
            qr = await accounts.begin_qr(user_id)
            if qr is None:
                await callback.message.answer("✅ Аккаунт подключён.", reply_markup=await get_campaigns_list_keyboard(user_id, user_id == owner_id))
                return
            buffer = BytesIO()
            qrcode.make(qr.url).save(buffer, format="PNG")
            keyboard = InlineKeyboardBuilder()
            keyboard.row(InlineKeyboardButton(text="📱 Подключить на этом телефоне", url=qr.url))
            keyboard.row(InlineKeyboardButton(text="« Назад", callback_data="back_to_campaigns"))
            await state.set_state(AccountLogin.qr)
            try:
                qr_message = await callback.message.answer_photo(
                    BufferedInputFile(buffer.getvalue(), filename="telegram-login.png"),
                    caption="Нажмите кнопку для подключения на этом телефоне или отсканируйте QR через Telegram: Настройки → Устройства → Подключить устройство.",
                    reply_markup=keyboard.as_markup(),
                )
            except TelegramBadRequest:
                qr_message = await callback.message.answer_photo(
                    BufferedInputFile(buffer.getvalue(), filename="telegram-login.png"),
                    caption="Отсканируйте QR через Telegram: Настройки → Устройства → Подключить устройство.",
                    reply_markup=get_back_to_campaigns_button(),
                )
            accounts.qr_tasks[user_id] = asyncio.create_task(wait_for_qr(qr, qr_message, state, user_id, owner_id))
        except Exception as exc:
            await accounts.cancel(user_id)
            await state.clear()
            logger.warning("QR authorization setup failed: %s", type(exc).__name__)
            await callback.message.answer("Не удалось создать QR-код. Попробуйте ещё раз.")


async def wait_for_qr(qr, qr_message: Message, state: FSMContext, user_id: int, owner_id: int):
    try:
        if not await qr.wait():
            raise ValueError("qr_not_authorized")
        async with accounts.lock(user_id):
            if user_id not in accounts.pending or accounts.pending[user_id][0] is not qr.client:
                return
            if user_id != owner_id and not await db.is_authorized_user(user_id):
                await accounts.cancel(user_id)
                await state.clear()
                return
            await accounts.activate(user_id, qr.client)
            await state.clear()
        await qr_message.answer("✅ Аккаунт подключён.", reply_markup=await get_campaigns_list_keyboard(user_id, user_id == owner_id))
    except SessionPasswordNeeded:
        async with accounts.lock(user_id):
            if user_id not in accounts.pending or accounts.pending[user_id][0] is not qr.client:
                return
            await state.set_state(AccountLogin.password)
        await qr_message.answer("Введите пароль двухэтапной проверки. Бот попытается удалить сообщение после получения.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("QR authorization failed: %s", type(exc).__name__)
        async with accounts.lock(user_id):
            if user_id in accounts.pending and accounts.pending[user_id][0] is qr.client:
                if isinstance(exc, ValueError) and str(exc) == "account_mismatch":
                    await accounts.reject_mismatched(user_id)
                else:
                    await accounts.cancel(user_id)
                await state.clear()
                await qr_message.answer("QR-код истёк или вход не завершён. Нажмите «Подключить аккаунт» ещё раз.", reply_markup=await get_campaigns_list_keyboard(user_id, user_id == owner_id))
    finally:
        if accounts.qr_tasks.get(user_id) is asyncio.current_task():
            accounts.qr_tasks.pop(user_id, None)
        try:
            await qr_message.delete()
        except Exception:
            pass


@router.message(AccountLogin.password)
async def account_password(message: Message, state: FSMContext, user_id: int, owner_id: int):
    if message.chat.type != "private":
        return
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        await message.answer("Не удалось удалить сообщение с паролем. Удалите его вручную.")
    async with accounts.lock(user_id):
        if user_id != owner_id and not await db.is_authorized_user(user_id):
            await accounts.cancel(user_id)
            await state.clear()
            return
        if user_id not in accounts.pending:
            await state.clear()
            await message.answer("Запрос истёк. Начните подключение заново.")
            return
        try:
            await accounts.password(user_id, password)
        except ValueError as exc:
            if str(exc) == "account_mismatch":
                await accounts.reject_mismatched(user_id)
            else:
                await accounts.cancel(user_id)
            await state.clear()
            await message.answer("Telegram ID аккаунта должен совпадать с вашим ID в боте.")
            return
        except Exception:
            await message.answer("Пароль не принят или запрос истёк. Попробуйте ещё раз.")
            return
        finally:
            password = None
        await state.clear()
    await message.answer("✅ Аккаунт подключён.", reply_markup=await get_campaigns_list_keyboard(user_id, user_id == owner_id))


@router.callback_query(F.data == "access_menu")
async def access_menu(callback: CallbackQuery, is_owner: bool):
    if not is_owner:
        await callback.answer("Доступно только главному администратору", show_alert=True)
        return
    await show_access_menu(callback)
    await callback.answer()


@router.callback_query(F.data == "access_add")
async def access_add(callback: CallbackQuery, state: FSMContext, is_owner: bool):
    if not is_owner:
        await callback.answer("Доступно только главному администратору", show_alert=True)
        return
    await state.set_state(AccessManagement.waiting_for_user_id)
    await callback.message.edit_text(
        "Отправьте числовой Telegram ID пользователя.",
        reply_markup=get_back_to_campaigns_button(),
    )
    await callback.answer()


@router.message(AccessManagement.waiting_for_user_id)
async def access_add_save(message: Message, state: FSMContext, is_owner: bool, owner_id: int):
    if not is_owner:
        await state.clear()
        await message.answer("Доступно только главному администратору")
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isascii() or not raw_id.isdecimal() or not 0 < int(raw_id) <= 2**63 - 1:
        await message.answer("Нужен положительный числовой Telegram ID.")
        return
    user_id = int(raw_id)
    await state.clear()
    if user_id == owner_id:
        await message.answer("Этот ID уже является главным администратором.")
        return
    added = await db.add_authorized_user(user_id)
    await message.answer(
        f"{'✅ Доступ выдан' if added else 'Этот ID уже имеет доступ'}: <code>{user_id}</code>",
        reply_markup=get_back_to_campaigns_button(),
    )
    if added:
        try:
            await message.bot.send_message(user_id, "✅ Доступ открыт. Отправьте /start, чтобы подключить свой аккаунт.")
        except Exception:
            await message.answer("Не удалось уведомить пользователя. Попросите его отправить /start.")


@router.callback_query(F.data.startswith("access_remove:"))
async def access_remove(callback: CallbackQuery, is_owner: bool):
    if not is_owner:
        await callback.answer("Доступно только главному администратору", show_alert=True)
        return
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID", show_alert=True)
        return
    async with accounts.lock(user_id):
        removed = await db.remove_authorized_user(user_id)
        await accounts.disconnect_user(user_id)
    await show_access_menu(callback)
    await callback.answer("Доступ удалён" if removed else "ID не найден")


# ===== CAMPAIGN MANAGEMENT =====

async def require_connected_account(message: Message, state: FSMContext) -> bool:
    if await accounts.is_connected(message.from_user.id):
        return True
    await state.clear()
    await message.answer("Сначала подключите свой Telegram-аккаунт через /start.")
    return False

@router.callback_query(F.data == "create_campaign")
async def create_campaign_start(callback: CallbackQuery, state: FSMContext):
    """Start campaign creation."""
    await callback.message.edit_text(
        "➕ <b>Создание кампании</b>\n\n"
        "Введите название для новой кампании:",
        reply_markup=get_back_to_campaigns_button(),
        parse_mode="HTML"
    )
    await state.set_state(CampaignCreation.waiting_for_name)
    await callback.answer()


@router.message(CampaignCreation.waiting_for_name)
async def create_campaign_save(message: Message, state: FSMContext, user_id: int):
    """Save new campaign."""
    if not await require_connected_account(message, state):
        return
    name = message.text.strip()
    
    if not name or len(name) > 100:
        await message.answer(
            "❌ Некорректное название!\n"
            "Введите название длиной от 1 до 100 символов:",
            reply_markup=get_back_to_campaigns_button()
        )
        return
    
    campaign_id = await db.create_campaign(name, user_id)
    await state.clear()
    
    await message.answer(
        f"✅ Кампания <b>'{name}'</b> создана!\n\n"
        f"ID: {campaign_id}",
        reply_markup=get_campaign_menu(campaign_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("campaign:"))
async def show_campaign(callback: CallbackQuery):
    """Show campaign details and menu."""
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)
    
    if not campaign:
        await callback.answer("❌ Кампания не найдена!", show_alert=True)
        return
    
    messages = await db.get_campaign_messages(campaign_id)
    groups = await db.get_campaign_groups(campaign_id)
    
    status = "🟢 Запущена" if campaign['is_running'] else "⚪️ Остановлена"
    
    text = (
        f"📊 <b>{campaign['name']}</b>\n\n"
        f"Статус: {status}\n"
        f"📝 Вариантов постов: {len(messages)}\n"
        f"👥 Групп: {len(groups)}\n"
        f"⏱ Интервал для каждой группы: {format_delay_range(campaign['min_delay'], campaign['max_delay'])}\n\n"
        f"Выберите действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_campaign_menu(campaign_id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_campaign:"))
async def delete_campaign(callback: CallbackQuery, is_owner: bool, user_id: int):
    """Delete a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)
    
    if not campaign:
        await callback.answer("❌ Кампания не найдена!", show_alert=True)
        return
    
    # Stop campaign if running
    if campaign['is_running']:
        await db.set_campaign_running(campaign_id, False)
    
    await db.delete_campaign(campaign_id)
    
    await callback.answer(f"🗑 Кампания '{campaign['name']}' удалена!", show_alert=True)
    
    # Return to campaigns list
    campaigns = await db.get_all_campaigns(user_id)
    text = "🤖 <b>Spam Bot - Мульти-кампании</b>\n\n"
    if campaigns:
        text += f"📊 Активных кампаний: {len(campaigns)}\n\nВыберите кампанию:"
    else:
        text += "Нет созданных кампаний.\nСоздайте первую кампанию!"
    
    keyboard = await get_campaigns_list_keyboard(user_id, is_owner)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


# ===== MESSAGE MANAGEMENT =====

@router.callback_query(F.data.startswith("add_message:"))
async def add_message_start(callback: CallbackQuery, state: FSMContext):
    """Start adding a message variant."""
    campaign_id = int(callback.data.split(":")[1])
    
    await state.update_data(campaign_id=campaign_id)
    await callback.message.edit_text(
        "📝 <b>Добавление варианта поста</b>\n\n"
        "Отправьте сообщение, которое нужно добавить.\n"
        "Можно отправить:\n"
        "• Только текст\n"
        "• Текст + фото\n"
        "• Текст + видео",
        reply_markup=get_back_to_campaign_button(campaign_id),
        parse_mode="HTML"
    )
    await state.set_state(MessageConfig.waiting_for_message)
    await callback.answer()


@router.message(MessageConfig.waiting_for_message)
async def add_message_save(message: Message, state: FSMContext):
    """Save message variant."""
    if not await require_connected_account(message, state):
        return
    data = await state.get_data()
    campaign_id = data['campaign_id']
    if not await db.campaign_belongs_to(campaign_id, message.from_user.id):
        await state.clear()
        await message.answer("Эта кампания вам недоступна")
        return
    
    text = message.caption if message.caption else message.text
    media_type = None
    media_id = None
    
    if message.photo:
        media_type = "photo"
        media_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_id = message.video.file_id
    elif message.text:
        media_type = "text"
    else:
        await message.answer(
            "❌ Неподдерживаемый тип сообщения.\n"
            "Отправьте текст, фото с текстом или видео с текстом.",
            reply_markup=get_back_to_campaign_button(campaign_id)
        )
        return
    
    message_id = await db.add_message(campaign_id, text, media_type, media_id)
    await state.clear()
    
    await message.answer(
        f"✅ Вариант поста добавлен! (ID: {message_id})",
        reply_markup=get_campaign_menu(campaign_id)
    )


@router.callback_query(F.data.startswith("list_messages:"))
async def list_messages(callback: CallbackQuery):
    """Show all message variants for a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    messages = await db.get_campaign_messages(campaign_id)
    
    if not messages:
        await callback.answer("📝 Нет добавленных вариантов постов", show_alert=True)
        return
    
    text = f"📝 <b>Варианты постов ({len(messages)})</b>\n\n"
    for idx, msg in enumerate(messages, 1):
        msg_type = {
            'text': '📄 Текст',
            'photo': '🖼 Фото',
            'video': '🎥 Видео'
        }.get(msg['media_type'], '❓')
        
        preview = msg['text'][:50] + "..." if msg['text'] and len(msg['text']) > 50 else msg['text']
        text += f"{idx}. {msg_type} (ID: {msg['id']})\n{preview}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_campaign_menu(campaign_id),
        parse_mode="HTML"
    )
    await callback.answer()


# ===== GROUPS MANAGEMENT =====

@router.callback_query(F.data.startswith("choose_groups:"))
async def choose_groups_start(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Выбор групп доступен в личном чате с ботом", show_alert=True)
        return
    campaign_id = int(callback.data.split(":", 1)[1])
    request_id = secrets.randbelow(2**31 - 1) + 1
    await state.update_data(campaign_id=campaign_id, group_select_request_id=request_id)
    await state.set_state(GroupsManagement.waiting_for_group_selection)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Выбрать группу", request_chat=KeyboardButtonRequestChat(
                request_id=request_id, chat_is_channel=False, request_title=True, request_username=True
            ))],
            [KeyboardButton(text="✅ Готово")],
        ], resize_keyboard=True,
    )
    await callback.message.answer(
        "Нажмите «Выбрать группу», выберите её в Telegram и подтвердите. "
        "Можно выбрать несколько групп по очереди. После этого нажмите «Готово».",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.message(GroupsManagement.waiting_for_group_selection, F.chat_shared)
async def choose_groups_save(message: Message, state: FSMContext, user_id: int, is_owner: bool):
    if message.chat.type != "private":
        return
    data = await state.get_data()
    shared = message.chat_shared
    campaign_id = data.get("campaign_id")
    if not campaign_id or shared.request_id != data.get("group_select_request_id"):
        await message.answer("Этот выбор устарел. Откройте выбор групп заново.")
        return
    async with accounts.lock(user_id):
        if not is_owner and not await db.is_authorized_user(user_id):
            await state.clear()
            return
        if not await db.campaign_belongs_to(campaign_id, user_id):
            await state.clear()
            await message.answer("Кампания недоступна.", reply_markup=ReplyKeyboardRemove())
            return
        try:
            client = await accounts.get_client(user_id)
            if not client:
                await message.answer("Подключите аккаунт заново через /start.")
                return
            found = None
            for archived in (False, True):
                async for dialog in client.get_dialogs(from_archive=archived):
                    if dialog.chat.id == shared.chat_id:
                        found = dialog.chat
                        break
                if found:
                    break
            if not found or getattr(found.type, "value", None) not in ("group", "supergroup", "forum"):
                await message.answer("Группа не найдена среди диалогов подключённого аккаунта. Проверьте, что он состоит в ней.")
                return
            await client.storage.save()
            current = await state.get_data()
            if current.get("campaign_id") != campaign_id or current.get("group_select_request_id") != shared.request_id:
                return
            existing = await db.get_campaign_groups(campaign_id)
            aliases = {str(shared.chat_id)}
            username = getattr(found, "username", None)
            if username:
                aliases.update({username.lower(), f"@{username.lower()}", f"https://t.me/{username.lower()}"})
            if any(item["group_link"].strip().lower() in aliases for item in existing):
                added = 0
            else:
                added = await db.add_group_for_owner(campaign_id, user_id, str(shared.chat_id))
                if added is None:
                    await state.clear()
                    await message.answer("Кампания недоступна.", reply_markup=ReplyKeyboardRemove())
                    return
        except Exception as exc:
            logger.warning("Group selection failed: %s", type(exc).__name__)
            await message.answer("Не удалось проверить группу. Попробуйте ещё раз.")
            return
    title = escape(getattr(found, "title", None) or str(shared.chat_id))
    await message.answer(
        f"{'✅ Добавлена' if added else 'Уже добавлена'}: <b>{title}</b>\n"
        "Выберите следующую группу или нажмите «Готово»."
    )


@router.message(GroupsManagement.waiting_for_group_selection, F.text == "✅ Готово")
async def choose_groups_finish(message: Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    await state.clear()
    await message.answer("Выбор групп завершён.", reply_markup=ReplyKeyboardRemove())
    if campaign_id and await db.campaign_belongs_to(campaign_id, user_id):
        await message.answer("👥 Управление группами", reply_markup=get_groups_menu(campaign_id))

@router.callback_query(F.data.startswith("groups_menu:"))
async def groups_menu(callback: CallbackQuery):
    """Show groups management menu."""
    campaign_id = int(callback.data.split(":")[1])
    groups = await db.get_campaign_groups(campaign_id)
    
    text = (
        f"👥 <b>Управление группами</b>\n\n"
        f"Всего групп: {len(groups)}\n\n"
        f"Выберите действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_groups_menu(campaign_id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("add_groups:"))
async def add_groups_start(callback: CallbackQuery, state: FSMContext):
    """Start adding groups."""
    campaign_id = int(callback.data.split(":")[1])
    
    await state.update_data(campaign_id=campaign_id)
    await callback.message.edit_text(
        "➕ <b>Добавление групп</b>\n\n"
        "Отправьте список групп (по одному на строку или через запятую).\n\n"
        "Форматы:\n"
        "• @groupname\n"
        "• https://t.me/groupname\n"
        "• -100123456789",
        reply_markup=get_back_to_campaign_button(campaign_id),
        parse_mode="HTML"
    )
    await state.set_state(GroupsManagement.waiting_for_groups)
    await callback.answer()


@router.message(GroupsManagement.waiting_for_groups)
async def add_groups_save(message: Message, state: FSMContext):
    """Save groups to campaign."""
    if not await require_connected_account(message, state):
        return
    data = await state.get_data()
    campaign_id = data['campaign_id']
    if not await db.campaign_belongs_to(campaign_id, message.from_user.id):
        await state.clear()
        await message.answer("Эта кампания вам недоступна")
        return
    
    text = message.text.strip()
    
    # Parse groups - support both newline and comma separation
    groups = []
    for line in text.split('\n'):
        for group in line.split(','):
            group = group.strip()
            if group:
                groups.append(group)
    
    if not groups:
        await message.answer(
            "❌ Не найдено групп в сообщении.",
            reply_markup=get_back_to_campaign_button(campaign_id)
        )
        return
    
    added_count = await db.add_groups(campaign_id, groups)
    await state.clear()
    
    await message.answer(
        f"✅ Добавлено групп: {added_count}\n"
        f"(Уже существовало: {len(groups) - added_count})",
        reply_markup=get_campaign_menu(campaign_id)
    )


@router.callback_query(F.data.startswith("show_groups:"))
async def show_groups(callback: CallbackQuery):
    """Display all groups for a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    groups = await db.get_campaign_groups(campaign_id)
    
    if not groups:
        await callback.answer("📋 Список групп пуст", show_alert=True)
        return
    
    text = "📋 <b>Список групп:</b>\n\n"
    for group in groups:
        text += f"• <code>{group['group_link']}</code>\n"
    
    text += f"\n<b>Всего:</b> {len(groups)}"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_groups_menu(campaign_id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("clear_groups:"))
async def clear_groups(callback: CallbackQuery):
    """Clear all groups for a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    count = await db.clear_campaign_groups(campaign_id)
    
    await callback.answer(f"🗑 Удалено групп: {count}", show_alert=True)
    await callback.message.edit_text(
        "👥 <b>Управление группами</b>\n\n"
        "✅ Все группы удалены.",
        reply_markup=get_groups_menu(campaign_id),
        parse_mode="HTML"
    )


# ===== DELAY CONFIGURATION =====

@router.callback_query(F.data.startswith("config_delay:"))
async def config_delay_start(callback: CallbackQuery, state: FSMContext):
    """Start delay configuration."""
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)
    
    if not campaign:
        await callback.answer("❌ Кампания не найдена!", show_alert=True)
        return
    
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Секунды", callback_data=f"delay_seconds:{campaign_id}"),
        InlineKeyboardButton(text="Минуты", callback_data=f"delay_minutes:{campaign_id}"),
    )
    builder.row(InlineKeyboardButton(text="« Назад", callback_data=f"campaign:{campaign_id}"))
    await callback.message.edit_text(
        "⏱ <b>Интервал для каждой группы</b>\n\n"
        f"Текущий интервал: {format_delay_range(campaign['min_delay'], campaign['max_delay'])}\n\n"
        "Выберите единицы интервала между повторными отправками в одну группу:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delay_seconds:") | F.data.startswith("delay_minutes:"))
async def config_delay_choose_unit(callback: CallbackQuery, state: FSMContext):
    action, campaign_id_text = callback.data.split(":", 1)
    campaign_id = int(campaign_id_text)
    unit = "мин" if action == "delay_minutes" else "сек"
    example = "30"
    await state.update_data(campaign_id=campaign_id, delay_unit=unit)
    await state.set_state(DelayConfig.waiting_for_delay)
    await callback.message.edit_text(
        f"⏱ Отправьте одно число в {unit} для точного интервала.\n"
        f"Пример: <code>{example}</code>\n\n"
        "Или отправьте два числа через пробел для случайного интервала.",
        reply_markup=get_back_to_campaign_button(campaign_id),
    )
    await callback.answer()


@router.message(DelayConfig.waiting_for_delay)
async def config_delay_save(message: Message, state: FSMContext):
    """Save delay configuration."""
    if not await require_connected_account(message, state):
        return
    data = await state.get_data()
    campaign_id = data['campaign_id']
    unit = data.get('delay_unit', 'сек')
    if not await db.campaign_belongs_to(campaign_id, message.from_user.id):
        await state.clear()
        await message.answer("Эта кампания вам недоступна")
        return
    
    try:
        parts = (message.text or "").strip().split()
        if len(parts) not in (1, 2):
            raise ValueError("Нужно одно число или два числа через пробел")
        
        multiplier = 60 if unit == 'мин' else 1
        min_delay = int(parts[0]) * multiplier
        max_delay = int(parts[-1]) * multiplier
        
        if min_delay <= 0 or max_delay <= 0:
            raise ValueError("Значения должны быть положительными")
        
        if min_delay > max_delay:
            raise ValueError("Минимум не может быть больше максимума")
        if max_delay > 2**31 - 1:
            raise ValueError("Слишком большой интервал")
        
        await db.update_campaign_delay(campaign_id, min_delay, max_delay)
        await state.clear()
        
        await message.answer(
            f"✅ Интервал для каждой группы: {format_delay_range(min_delay, max_delay)}",
            reply_markup=get_campaign_menu(campaign_id)
        )
        
    except ValueError as e:
        await message.answer(
            f"❌ Ошибка: {str(e)}\n\n"
            f"Отправьте одно или два положительных целых числа в {unit}.\n"
            "Пример: <code>30</code> или <code>30 45</code>",
            reply_markup=get_back_to_campaign_button(campaign_id),
            parse_mode="HTML"
        )


# ===== START/STOP CAMPAIGN =====

@router.callback_query(F.data.startswith("start_campaign:"))
async def start_campaign(callback: CallbackQuery, user_id: int, owner_id: int):
    """Start a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)
    
    if not campaign:
        await callback.answer("❌ Кампания не найдена!", show_alert=True)
        return
    
    # Check if already running
    if campaign['is_running']:
        await callback.answer("⚠️ Кампания уже запущена!", show_alert=True)
        return
    
    # Validate configuration
    messages = await db.get_campaign_messages(campaign_id)
    groups = await db.get_campaign_groups(campaign_id)
    
    if not messages:
        await callback.answer("❌ Нет вариантов постов!", show_alert=True)
        return
    
    if not groups:
        await callback.answer("❌ Список групп пуст!", show_alert=True)
        return
    
    async with accounts.lock(user_id):
        if user_id != owner_id and not await db.is_authorized_user(user_id):
            await callback.answer("Доступ отозван", show_alert=True)
            return
        remaining = await flood_wait_remaining(user_id)
        if remaining:
            await callback.answer(f"Telegram ограничил отправку ещё на {remaining} сек", show_alert=True)
            return
        try:
            client = await accounts.get_client(user_id)
        except Exception:
            await callback.answer("Не удалось подключить аккаунт. Повторите позже.", show_alert=True)
            return
        if not client:
            await callback.answer("Сначала подключите свой Telegram-аккаунт", show_alert=True)
            return
        current = await db.get_campaign(campaign_id)
        if not current or current['owner_id'] != user_id:
            await callback.answer("Кампания недоступна", show_alert=True)
            return
        if current['is_running']:
            await callback.answer("⚠️ Кампания уже запущена!", show_alert=True)
            return
        if not await db.start_campaign_fresh(campaign_id):
            await callback.answer("⚠️ Кампания уже запущена или недоступна!", show_alert=True)
            return
    
    await callback.answer("▶️ Кампания запущена!", show_alert=True)
    await callback.message.edit_text(
        f"🚀 <b>{campaign['name']}</b>\n\n"
        f"✅ Рассылка запущена!\n\n"
        f"📝 Вариантов постов: {len(messages)}\n"
        f"👥 Групп: {len(groups)}\n"
        f"⏱ Интервал для каждой группы: {format_delay_range(campaign['min_delay'], campaign['max_delay'])}\n\n"
        "Рассылка идет с ротацией постов...\n\n"
        "Используйте кнопку СТОП для остановки.",
        reply_markup=get_campaign_menu(campaign_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("stop_campaign:"))
async def stop_campaign(callback: CallbackQuery):
    """Stop a campaign."""
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)
    
    if not campaign:
        await callback.answer("❌ Кампания не найдена!", show_alert=True)
        return
    
    if not campaign['is_running']:
        await callback.answer("⚠️ Кампания не запущена!", show_alert=True)
        return
    
    await db.set_campaign_running(campaign_id, False)
    
    await callback.answer("⏹ Кампания остановлена!", show_alert=True)
    await callback.message.edit_text(
        f"⏹ <b>{campaign['name']}</b>\n\n"
        "Рассылка остановлена.\n\n"
        "Вы можете запустить её снова когда будет нужно.",
        reply_markup=get_campaign_menu(campaign_id),
        parse_mode="HTML"
    )
