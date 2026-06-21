import pytest
from pydantic import ValidationError

import providers as providers_module
from api.routes.jobs import RecordRequest
from api.routes.meetings import MeetingCreate, MeetingUpdate
from providers import get_provider_metadata, list_provider_metadata, list_providers, provider_form_config_map
from telegram_bot.keyboards import get_provider_keyboard


def test_provider_registry_metadata_is_single_source_for_supported_providers():
    providers = list_providers()
    metadata = list_provider_metadata()

    assert providers == ["jitsi", "webex", "zoom"]
    assert [provider.name for provider in metadata] == providers
    assert get_provider_metadata("zoom").meeting_code_label == "Meeting URL / ID"
    assert provider_form_config_map()["zoom"]["hint"] == "Full Zoom invite link is recommended"
    assert not hasattr(providers_module, "provider_metadata_map")


def test_api_provider_validators_normalize_and_reject_unknown_provider():
    assert RecordRequest(provider="Zoom", meeting_code="https://zoom.us/j/123", duration_sec=60).provider == "zoom"
    assert MeetingCreate(name="Zoom Standup", provider="ZOOM", meeting_code="https://zoom.us/j/123").provider == "zoom"
    assert MeetingUpdate(provider="Webex").provider == "webex"

    with pytest.raises(ValidationError):
        RecordRequest(provider="teams", meeting_code="abc", duration_sec=60)

    with pytest.raises(ValidationError):
        MeetingCreate(name="Teams Meeting", provider="teams", meeting_code="abc")


def test_api_provider_schema_uses_registry_enum():
    assert RecordRequest.model_json_schema()["properties"]["provider"]["enum"] == list_providers()
    assert MeetingCreate.model_json_schema()["properties"]["provider"]["enum"] == list_providers()


def test_telegram_provider_keyboard_uses_registry_metadata():
    keyboard = get_provider_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    provider_buttons = [button for button in buttons if button.callback_data.startswith("provider:")]

    assert [button.text for button in provider_buttons] == [provider.label for provider in list_provider_metadata()]
    assert [button.callback_data for button in provider_buttons] == [
        f"provider:{provider.name}" for provider in list_provider_metadata()
    ]
