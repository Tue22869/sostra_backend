import os

import requests
import structlog
from django.conf import settings


SMSAERO_API_URL = "https://gate.smsaero.ru/v2/sms/send"
logger = structlog.get_logger(__name__)


def send_sms(phone: str, message: str) -> bool:
    """
    Отправляет SMS сообщение.
    Поддерживает различные SMS-провайдеры через переменные окружения.
    
    Для настройки используйте переменные окружения:
    - SMS_PROVIDER: только 'smsaero'
    - SMS_API_KEY: API ключ провайдера
    """
    provider = os.getenv('SMS_PROVIDER', 'smsaero').lower()
    api_key = os.getenv('SMS_API_KEY', '')
    
    if not api_key:
        logger.warning("sms_send_skipped_missing_api_key", phone=phone)
        return False
    
    try:
        if provider == 'smsaero':
            return _send_sms_smsaero(phone[1:], message, api_key)
        else:
            logger.warning("sms_send_skipped_unknown_provider", provider=provider, phone=phone)
            return False
    except Exception:
        logger.exception("sms_send_failed", phone=phone, provider=provider)
        return False


def _send_sms_smsaero(
        phone: str,
        message: str,
        api_key: str
) -> bool:
    """
    Отправляет SMS через API SMS Aero.

    Аргументы:
    - phone: номер получателя в формате 7XXXXXXXXXX
    - message: текст SMS
    - api_key: ваш API ключ из кабинета SMS Aero
    - email: email, с которым зарегистрирован API ключ
    - sender: имя отправителя (numeric или текст, если подтверждено)

    Возвращает True если сообщение успешно отправлено.
    """

    email = os.getenv('SMSAERO_EMAIL', '')
    sender = os.getenv('SMSAERO_SENDER', 'SMS Aero')

    payload = {
        "number": phone,
        "text": message,
        "sign": sender,
        "channel": "digital"
    }

    resp = requests.post(
        SMSAERO_API_URL,
        json=payload,
        auth=(email, api_key),
        timeout=15
    )

    try:
        data = resp.json()
    except ValueError:
        logger.warning("sms_provider_invalid_json", response_text=resp.text, phone=phone)
        return False

    if data.get("success"):
        logger.info("sms_send_finished", phone=phone, provider="smsaero")
        return True
    logger.warning("sms_send_rejected_by_provider", phone=phone, provider="smsaero", provider_response=data)
    return False
