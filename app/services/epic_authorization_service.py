"""Bounded login state machine. A redirect or a click is not authentication."""
import asyncio
import re
import time
from urllib.parse import urlsplit

from loguru import logger
from playwright.async_api import Page

from settings import settings, HCAPTCHA_DIR, LOG_DIR

URL_CLAIM = 'https://store.epicgames.com/en-US/free-games'
URL_LOGIN = 'https://www.epicgames.com/account/personal?lang=en-US'


class AuthenticationError(RuntimeError):
    pass


class ManualActionRequired(AuthenticationError):
    pass


async def first_visible(locator):
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await item.is_visible():
            return item
    return None


async def store_authenticated(page: Page, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nav = page.locator('egs-navigation').first
        if await nav.count() and await nav.get_attribute('isloggedin') == 'true':
            return True
        await asyncio.sleep(0.3)
    return False


async def interactive_challenge(page: Page) -> bool:
    # A hidden/background CAPTCHA iframe alone is not an active challenge.
    for frame in page.frames:
        if 'hcaptcha.com' in frame.url and 'frame=challenge' in frame.url:
            element = await frame.frame_element()
            if await element.is_visible():
                return True
    otp = page.locator('input[autocomplete="one-time-code"], input[name="code"]')
    if await first_visible(otp):
        return True
    return bool(await first_visible(page.get_by_text(re.compile(
        r'请完成安全检查|Verify you are human|Complete the security check|Checking your browser',
        re.I,
    ))))


async def hcaptcha_visible(page):
    for frame in page.frames:
        if 'hcaptcha.com' in frame.url and 'frame=challenge' in frame.url:
            if await (await frame.frame_element()).is_visible():
                return True
    return False


def make_challenge_agent(page):
    from services.new_api_adapter import install_new_api_adapter, NewAPIClient
    NewAPIClient(settings)  # Validate configuration before installing handlers.
    from hcaptcha_challenger.agent import AgentV, AgentConfig
    install_new_api_adapter(settings)
    # The third-party package initializes its own Loguru sinks on first import.
    # Restore redacted application logging after importing it.
    from utils import init_log
    init_log(runtime=LOG_DIR / 'runtime.log', error=LOG_DIR / 'error.log')
    config = AgentConfig(
        # Legacy internal field name only; requests go exclusively to New API.
        # AgentConfig's before-validator requires str, not SecretStr.
        # Pydantic wraps the accepted value back into SecretStr internally.
        GEMINI_API_KEY=settings.NEW_API_API_KEY.get_secret_value(),
        cache_dir=HCAPTCHA_DIR / '.cache',
        challenge_dir=HCAPTCHA_DIR / '.challenge',
        captcha_response_dir=HCAPTCHA_DIR / '.captcha',
        EXECUTION_TIMEOUT=120,
        RESPONSE_TIMEOUT=30,
        RETRY_ON_FAILURE=False,
        DISABLE_BEZIER_TRAJECTORY=True,
        CONSTRAINT_RESPONSE_SCHEMA=True,
    )
    return AgentV(page=page, agent_config=config)


async def solve_challenge(agent):
    logger.info('Handling visible hCaptcha with the configured provider')
    try:
        signal = await asyncio.wait_for(agent.wait_for_challenge(), settings.CAPTCHA_TIMEOUT_SECONDS)
        from hcaptcha_challenger.models import ChallengeSignal
        if signal != ChallengeSignal.SUCCESS:
            raise AuthenticationError('hCaptcha handler did not report success')
    except asyncio.TimeoutError as exc:
        raise AuthenticationError('hCaptcha handler timed out') from exc


class EpicAuthorization:
    def __init__(self, page: Page, manual: bool = False):
        self.page = page
        self.manual = manual
        self.failure = None
        self.challenge_agent = None
        self.challenge_attempts = 0

    async def _response(self, response):
        parsed = urlsplit(response.url)
        if parsed.hostname != 'www.epicgames.com' or '/id/api/' not in parsed.path:
            return
        if response.status in (403, 429):
            self.failure = f'Authentication request rejected: HTTP {response.status}'
        if response.request.method == 'POST':
            try:
                data = await response.json()
                if isinstance(data, dict) and data.get('errorCode'):
                    # Do not log response bodies, account identifiers, or tokens.
                    code = str(data['errorCode'])
                    if not any(word in code.lower() for word in ('captcha', 'challenge')):
                        self.failure = 'Epic authentication error: ' + code
            except (ValueError, TypeError):
                pass

    async def _handle_challenge(self):
        if await hcaptcha_visible(self.page):
            if self.challenge_attempts >= 2:
                raise AuthenticationError('hCaptcha did not complete after two attempts')
            self.challenge_attempts += 1
            if self.challenge_agent is None:
                self.challenge_agent = make_challenge_agent(self.page)
            await solve_challenge(self.challenge_agent)
            return True
        if await interactive_challenge(self.page):
            raise ManualActionRequired('Epic requires 2FA or a security check unsupported by the solver')
        return False

    async def _fill(self, field, value):
        await field.fill(value)
        await field.press('Tab')
        if await field.input_value() != value:
            raise AuthenticationError('Input value did not persist after filling')

    async def _submit(self, field):
        # Scope to the ACTIVE input's form. Continue may legitimately submit
        # either stage; never blacklist it, force-enable it, or use Enter on failure.
        form = field.locator('xpath=ancestor::form[1]')
        scope = form if await form.count() else self.page
        candidates = scope.locator('button[type="submit"], #continue, #sign-in')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.failure:
                raise AuthenticationError(self.failure)
            if await self._handle_challenge():
                deadline = time.monotonic() + 20
                continue
            visible = [b for b in await candidates.all() if await b.is_visible()]
            enabled = [b for b in visible if await b.is_enabled()]
            if len(enabled) == 1:
                await enabled[0].click(timeout=5000)
                return
            if len(enabled) > 1:
                raise AuthenticationError('Multiple submit buttons in active form; refusing to guess')
            await asyncio.sleep(0.3)
        invalid = await scope.locator('input:invalid, input[aria-invalid="true"]').count()
        raise AuthenticationError(
            f'Active form submit stayed disabled (invalid inputs: {invalid}); '
            'inspect the page in --login mode'
        )

    async def invoke(self):
        await self.page.goto(URL_CLAIM, wait_until='domcontentloaded')
        if await store_authenticated(self.page):
            logger.success('AUTHENTICATED: store reports isloggedin=true')
            return True
        if self.manual:
            await self.page.goto(URL_LOGIN, wait_until='domcontentloaded')
            logger.info('Complete login, 2FA and any verification in the opened browser. '
                        'After login, return to the Epic Store free-games page in this same tab.')
            deadline = time.monotonic() + settings.MANUAL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                parsed = urlsplit(self.page.url)
                if parsed.hostname == 'store.epicgames.com' and await store_authenticated(self.page, 1):
                    return True
                await asyncio.sleep(0.5)
            raise AuthenticationError('Manual login timed out; session not verified')
        await self._handle_challenge()
        if not settings.EPIC_EMAIL or not settings.EPIC_PASSWORD.get_secret_value():
            raise AuthenticationError('No valid session. Run --login locally or configure credentials')
        # Initialize before navigation/submission so the solver observes challenge traffic.
        if self.challenge_agent is None:
            self.challenge_agent = make_challenge_agent(self.page)
        self.page.on('response', self._response)
        try:
            await self.page.goto(URL_LOGIN, wait_until='domcontentloaded')
            email_sent = password_sent = False
            deadline = time.monotonic() + settings.LOGIN_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self.failure:
                    raise AuthenticationError(self.failure)
                if await self._handle_challenge():
                    deadline = time.monotonic() + settings.LOGIN_TIMEOUT_SECONDS
                    continue
                parsed = urlsplit(self.page.url)
                if parsed.hostname == 'store.epicgames.com' and await store_authenticated(self.page, 1):
                    return True
                if parsed.hostname == 'www.epicgames.com' and parsed.path.startswith('/account'):
                    await self.page.goto(URL_CLAIM, wait_until='domcontentloaded')
                    if await store_authenticated(self.page):
                        return True
                    raise AuthenticationError('Account redirect completed but store is not authenticated')
                email = await first_visible(self.page.locator('#email, input[type="email"]'))
                password = await first_visible(self.page.locator('#password, input[type="password"]'))
                if password is not None and not password_sent:
                    if email is not None:
                        await self._fill(email, settings.EPIC_EMAIL)
                    await self._fill(password, settings.EPIC_PASSWORD.get_secret_value())
                    await self._submit(password)
                    password_sent = True
                elif email is not None and not email_sent and not password_sent:
                    await self._fill(email, settings.EPIC_EMAIL)
                    await self._submit(email)
                    email_sent = True
                await asyncio.sleep(0.3)
            raise AuthenticationError('Login timed out without a verified store session')
        finally:
            self.page.remove_listener('response', self._response)
            if self.challenge_agent is not None:
                self.page.remove_listener('response', self.challenge_agent._task_handler)
