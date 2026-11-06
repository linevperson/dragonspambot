"""Operator-facing status without confusing an attempt with successful delivery."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def format_attempt_time(timestamp):
    if timestamp is None:
        return "ещё не было"
    return datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")


def format_campaign_status(snapshot, sending=False, now=None, safety_blocked=False, storage_blocked=False):
    now = time.time() if now is None else now
    running = bool(snapshot["is_running"])
    blocked = snapshot["flood_until"] > now or safety_blocked
    next_time = "—"
    if storage_blocked:
        status = "🛑 Остановлена: ошибка сохранения базы"
    elif blocked and (running or snapshot.get("stop_reason") == "flood" or safety_blocked):
        status = "🛑 Остановлена ограничением Telegram"
    elif not running:
        status = {
            "flood": "🛑 Остановлена после ограничения Telegram — можно запустить вручную",
            "restart": "⚪️ Остановлена после перезапуска бота",
            "account": "🔑 Остановлена: аккаунт не подключён",
            "access_removed": "🔒 Остановлена: доступ отозван",
            "storage": "🛑 Остановлена: ошибка сохранения базы",
            "restricted": "🛑 Остановлена: ограничение или потеря авторизации аккаунта",
        }.get(snapshot.get("stop_reason"), "⚪️ Остановлена")
    elif sending:
        status = "📤 Отправляет"
        next_time = "текущая попытка выполняется"
    elif not snapshot["message_count"]:
        status = "📝 Нет доступных постов"
    elif not snapshot["group_count"]:
        status = "👥 Нет групп"
    elif not snapshot["enabled_group_count"]:
        status = "⏸ Все группы выключены"
    else:
        due = snapshot["next_attempt_at"]
        status = "⏳ Ожидает следующую отправку" if due > now else "⏳ Ожидает отправки"
        next_time = format_attempt_time(due) if due > now else "сейчас / в очереди"
    text = (
        f"Статус: {status}\n"
        f"Последняя попытка: {format_attempt_time(snapshot['last_attempt_at'])}\n"
        f"Ближайшая попытка: {next_time}\n"
        "Время: Киев. Попытка не означает успешную доставку."
    )
    if blocked:
        text += "\nЗапуск аккаунта заблокирован"
        if not safety_blocked:
            text += f" до {format_attempt_time(snapshot['flood_until'])}"
        text += ". Автоматического возобновления нет."
    return text
