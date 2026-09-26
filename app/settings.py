"""Public settings use New API's OpenAI-compatible interface only."""
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent
VOLUMES_DIR = PROJECT_ROOT / 'volumes'
LOG_DIR = VOLUMES_DIR / 'logs'
RUNTIME_DIR = VOLUMES_DIR / 'runtime'
SCREENSHOTS_DIR = VOLUMES_DIR / 'screenshots'
RECORD_DIR = VOLUMES_DIR / 'record'
USER_DATA_DIR = VOLUMES_DIR / 'user_data'
HCAPTCHA_DIR = VOLUMES_DIR / 'hcaptcha'


class EpicSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_ignore_empty=True, extra='ignore')
    EPIC_EMAIL: str = ''
    EPIC_PASSWORD: SecretStr = SecretStr('')
    EPIC_COUNTRY: str = 'CN'
    NEW_API_BASE_URL: str = ''
    NEW_API_API_KEY: SecretStr = SecretStr('')
    NEW_API_MODEL: str = ''
    NEW_API_TIMEOUT: float = 90
    NEW_API_RESPONSE_FORMAT: Literal['prompt', 'json_object', 'json_schema'] = 'json_object'
    HEADLESS: bool = True
    ENABLE_APSCHEDULER: bool = False
    TASK_TIMEOUT_SECONDS: int = 900
    LOGIN_TIMEOUT_SECONDS: int = 180
    MANUAL_TIMEOUT_SECONDS: int = 600
    CAPTCHA_TIMEOUT_SECONDS: int = 180
    SAVE_SCREENSHOTS: bool = False
    REDIS_URL: str = 'redis://redis:6379/0'
    CELERY_WORKER_CONCURRENCY: int = 1
    CELERY_TASK_TIME_LIMIT: int = 1200
    CELERY_TASK_SOFT_TIME_LIMIT: int = 900

    @property
    def user_data_dir(self):
        name = hashlib.sha256(self.EPIC_EMAIL.strip().lower().encode()).hexdigest()[:16]
        path = USER_DATA_DIR / name
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = EpicSettings()
