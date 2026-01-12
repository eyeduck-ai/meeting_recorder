"""Telegram keyboard definitions."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Get the main menu reply keyboard (persistent at bottom)."""
    keyboard = [
        [KeyboardButton("📋 查看排程"), KeyboardButton("➕ 新增排程")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_meetings_inline_keyboard(meetings: list) -> InlineKeyboardMarkup:
    """Get inline keyboard for meeting selection."""
    buttons = []
    for meeting in meetings:
        provider = meeting.provider.upper() if hasattr(meeting.provider, "upper") else str(meeting.provider).upper()
        buttons.append(
            [InlineKeyboardButton(f"{meeting.name} ({provider})", callback_data=f"select_meeting:{meeting.id}")]
        )
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def get_time_inline_keyboard() -> InlineKeyboardMarkup:
    """Get inline keyboard for quick time selection."""
    buttons = [
        [
            InlineKeyboardButton("現在", callback_data="time:now"),
            InlineKeyboardButton("+15分", callback_data="time:15"),
            InlineKeyboardButton("+30分", callback_data="time:30"),
        ],
        [
            InlineKeyboardButton("+1小時", callback_data="time:60"),
            InlineKeyboardButton("+2小時", callback_data="time:120"),
        ],
        [
            InlineKeyboardButton("📅 自訂時間", callback_data="time:custom"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def get_duration_inline_keyboard() -> InlineKeyboardMarkup:
    """Get inline keyboard for duration selection."""
    buttons = [
        [
            InlineKeyboardButton("30 分鐘", callback_data="duration:30"),
            InlineKeyboardButton("60 分鐘", callback_data="duration:60"),
        ],
        [
            InlineKeyboardButton("90 分鐘", callback_data="duration:90"),
            InlineKeyboardButton("120 分鐘", callback_data="duration:120"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def get_confirm_keyboard() -> InlineKeyboardMarkup:
    """Get confirmation inline keyboard."""
    buttons = [
        [
            InlineKeyboardButton("✅ 確認建立", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_schedule_actions_keyboard(schedule_id: int) -> InlineKeyboardMarkup:
    """Get inline keyboard for schedule actions."""
    buttons = [
        [
            InlineKeyboardButton("▶️ 立即執行", callback_data=f"trigger:{schedule_id}"),
            InlineKeyboardButton("🔄 切換狀態", callback_data=f"toggle:{schedule_id}"),
        ],
        [InlineKeyboardButton("🔙 返回列表", callback_data="back_to_list")],
    ]
    return InlineKeyboardMarkup(buttons)
