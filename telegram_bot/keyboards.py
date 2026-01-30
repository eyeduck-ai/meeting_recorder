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


def get_youtube_inline_keyboard() -> InlineKeyboardMarkup:
    """Get inline keyboard for YouTube upload option."""
    buttons = [
        [
            InlineKeyboardButton("是 (unlisted)", callback_data="youtube:unlisted"),
            InlineKeyboardButton("是 (private)", callback_data="youtube:private"),
        ],
        [
            InlineKeyboardButton("否", callback_data="youtube:no"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def get_schedules_select_keyboard(schedules: list, tz: str) -> InlineKeyboardMarkup:
    """Get inline keyboard for schedule selection (edit mode)."""
    from utils.timezone import to_local

    buttons = []
    for s in schedules:
        local_time = to_local(s.next_run_at, tz) if s.next_run_at else None
        time_str = local_time.strftime("%m/%d %H:%M") if local_time else "-"
        label = f"{s.meeting.name} ({time_str})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"edit_schedule:{s.id}")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def get_edit_time_keyboard() -> InlineKeyboardMarkup:
    """Get inline keyboard for editing time."""
    buttons = [
        [
            InlineKeyboardButton("+15分", callback_data="edit_time:15"),
            InlineKeyboardButton("+30分", callback_data="edit_time:30"),
            InlineKeyboardButton("+1小時", callback_data="edit_time:60"),
        ],
        [
            InlineKeyboardButton("📅 自訂時間", callback_data="edit_time:custom"),
        ],
        [
            InlineKeyboardButton("🗑️ 刪除排程", callback_data="edit_time:delete"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def get_edit_confirm_keyboard() -> InlineKeyboardMarkup:
    """Get confirmation keyboard for edit."""
    buttons = [
        [
            InlineKeyboardButton("✅ 確認修改", callback_data="edit_confirm:yes"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_delete_confirm_keyboard() -> InlineKeyboardMarkup:
    """Get confirmation keyboard for delete."""
    buttons = [
        [
            InlineKeyboardButton("🗑️ 確認刪除", callback_data="edit_confirm:delete"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_meetings_list_keyboard(meetings: list) -> InlineKeyboardMarkup:
    """Get inline keyboard for meeting list with add button."""
    buttons = []
    for meeting in meetings:
        provider = meeting.provider.upper() if hasattr(meeting.provider, "upper") else str(meeting.provider).upper()
        buttons.append(
            [InlineKeyboardButton(f"{meeting.name} ({provider})", callback_data=f"view_meeting:{meeting.id}")]
        )
    buttons.append([InlineKeyboardButton("➕ 新增會議", callback_data="add_meeting")])
    buttons.append([InlineKeyboardButton("❌ 關閉", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def get_provider_keyboard() -> InlineKeyboardMarkup:
    """Get inline keyboard for provider selection."""
    buttons = [
        [
            InlineKeyboardButton("Jitsi", callback_data="provider:jitsi"),
            InlineKeyboardButton("Webex", callback_data="provider:webex"),
        ],
        [
            InlineKeyboardButton("Zoom", callback_data="provider:zoom"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


def get_meeting_confirm_keyboard() -> InlineKeyboardMarkup:
    """Get confirmation keyboard for meeting creation."""
    buttons = [
        [
            InlineKeyboardButton("✅ 確認新增", callback_data="meeting_confirm:yes"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)
