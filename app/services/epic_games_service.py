"""Claim active zero-price offers and verify ownership on each product page."""
import asyncio
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx
from loguru import logger

from models import PromotionGame
from services.epic_authorization_service import (
    AuthenticationError, ManualActionRequired, first_visible,
    interactive_challenge, store_authenticated, URL_CLAIM,
    hcaptcha_visible, make_challenge_agent, solve_challenge,
)
from settings import settings

URL_PROMOTIONS = 'https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions'
OWNED = re.compile(r'^(in library|owned|已在库中|已拥有)$', re.I)
GET = re.compile(r'^(get|free|获取|免费获取|领取)$', re.I)
PLACE_ORDER = re.compile(r'^(place order|下单|下訂單)$', re.I)


def parse_promotions(data, now=None):
    now = now or datetime.now(timezone.utc)
    elements = data['data']['Catalog']['searchStore']['elements']
    result = []
    seen = set()
    for item in elements:
        price = item.get('price', {}).get('totalPrice', {})
        if price.get('discountPrice') != 0 or not price.get('originalPrice', 0) > 0:
            continue
        active = False
        for group in (item.get('promotions') or {}).get('promotionalOffers', []):
            for offer in group.get('promotionalOffers', []):
                try:
                    start = datetime.fromisoformat(offer['startDate'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(offer['endDate'].replace('Z', '+00:00'))
                    active |= (start <= now < end and
                               offer['discountSetting']['discountPercentage'] == 0)
                except (KeyError, TypeError, ValueError):
                    continue
        if not active:
            continue
        mappings = item.get('offerMappings') or item.get('catalogNs', {}).get('mappings') or []
        mapping = next((m for m in mappings if m.get('pageSlug')), {})
        slug = mapping.get('pageSlug') or item.get('productSlug') or item.get('urlSlug')
        if not slug:
            raise RuntimeError(f"No product mapping for offer {item.get('id')}")
        # Never infer a bundle from the marketing title (e.g. 'Collection').
        bundle = mapping.get('pageType') == 'bundle' or item.get('offerType') == 'BUNDLE'
        route = 'bundles' if bundle else 'p'
        key = (item['namespace'], item['id'])
        if key in seen:
            continue
        seen.add(key)
        result.append(PromotionGame(
            title=item['title'], id=item['id'], namespace=item['namespace'],
            description=item.get('description', ''), offerType=item.get('offerType', ''),
            url=f"https://store.epicgames.com/en-US/{route}/{quote(slug, safe='/-')}",
        ))
    return result


def get_promotions():
    response = httpx.get(URL_PROMOTIONS, params={
        'locale': 'en-US', 'country': settings.EPIC_COUNTRY,
        'allowCountries': settings.EPIC_COUNTRY,
    }, timeout=30)
    response.raise_for_status()
    return parse_promotions(response.json())


def zero_total(text):
    """Only accept an explicit currency amount or 'Free'; reject ambiguous text."""
    text = text.strip()
    if text.lower() in ('free', '免费', '免費'):
        return True
    match = re.fullmatch(r'(?:[A-Z]{3}\s*|[¥￥$€£]\s*)?([0-9]+(?:[.,][0-9]{1,2})?)'
                         r'(?:\s*[A-Z]{3})?', text)
    if not match:
        return False
    try:
        return Decimal(match[1].replace(',', '.')) == 0
    except InvalidOperation:
        return False


class EpicGames:
    def __init__(self, page, manual=False):
        self.page = page
        self.manual = manual
        self.challenge_agent = None

    async def _cta(self):
        locator = self.page.get_by_test_id('purchase-cta-button')
        await locator.first.wait_for(state='visible', timeout=30000)
        if await locator.count() != 1:
            raise RuntimeError('Ambiguous product CTA; refusing to choose an edition')
        return locator.first

    async def _owned(self):
        cta = await self._cta()
        return bool(OWNED.fullmatch((await cta.inner_text()).strip()))

    async def _checkout(self, promotion):
        deadline = time.monotonic() + (settings.MANUAL_TIMEOUT_SECONDS if self.manual else 45)
        submitted = False
        challenge_attempts = 0
        while time.monotonic() < deadline:
            if await hcaptcha_visible(self.page):
                if challenge_attempts >= 2:
                    raise RuntimeError('Checkout hCaptcha did not complete after two attempts')
                challenge_attempts += 1
                if self.challenge_agent is None:
                    self.challenge_agent = make_challenge_agent(self.page)
                await solve_challenge(self.challenge_agent)
                deadline = time.monotonic() + 45
                continue
            if await interactive_challenge(self.page):
                if not self.manual:
                    raise ManualActionRequired('Checkout requires interactive verification')
                await asyncio.sleep(1)
                continue
            # EULA, age prompts and optional account notices are left to the user.
            for frame in self.page.frames:
                button = await first_visible(frame.get_by_role('button', name=PLACE_ORDER))
                if button is None or submitted:
                    continue
                body = await frame.locator('body').inner_text()
                if promotion.title.casefold() not in body.casefold():
                    raise RuntimeError('Checkout product title does not match the requested game')
                # The value must directly follow a Total label, not a discount/subtotal.
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                totals = []
                for index, line in enumerate(lines):
                    if re.fullmatch(r'Total|总计|總計|合计', line, re.I) and index + 1 < len(lines):
                        totals.append(lines[index + 1])
                    same_line = re.fullmatch(r'(?:Total|总计|總計|合计)\s*[:：]?\s+(.+)', line, re.I)
                    if same_line:
                        totals.append(same_line[1])
                if not totals or not all(zero_total(value) for value in totals):
                    raise RuntimeError('Checkout total is nonzero or cannot be verified; order not submitted')
                await button.click(timeout=15000)
                submitted = True
                logger.info('Order submitted; waiting to verify ownership')
            if submitted:
                # Give checkout time to finish; never resubmit an uncertain order.
                success = self.page.get_by_text(re.compile(
                    r'Thank you for (your purchase|buying)|感谢.*购买|感謝.*購買', re.I))
                if await first_visible(success) or '/success' in self.page.url:
                    break
                purchase_frames = [f for f in self.page.frames if 'purchase' in f.url.lower()]
                if not purchase_frames:
                    break
            await asyncio.sleep(0.5)
        if not submitted:
            raise ManualActionRequired('Checkout not available: inspect license/age/region prompts with --interactive')
        # A missing iframe or success URL is only a reason to VERIFY, never proof.
        for _ in range(3):
            await self.page.goto(promotion.url, wait_until='domcontentloaded')
            if await self._owned():
                return
            await asyncio.sleep(2)
        raise RuntimeError('Order outcome unverified: product CTA is not In Library; no automatic resubmit')

    async def collect_weekly_games(self, promotions):
        results = []
        for promotion in promotions:
            await self.page.goto(promotion.url, wait_until='domcontentloaded')
            cta = await self._cta()
            label = (await cta.inner_text()).strip()
            if OWNED.fullmatch(label):
                results.append({'title': promotion.title, 'status': 'already_owned'})
                logger.success('ALREADY_OWNED: {}', promotion.title)
                continue
            if not GET.fullmatch(label):
                raise RuntimeError(f'Unexpected CTA {label!r} for {promotion.title}; no purchase attempted')
            self.challenge_agent = make_challenge_agent(self.page)
            await cta.click(timeout=15000)
            try:
                await self._checkout(promotion)
            finally:
                self.page.remove_listener('response', self.challenge_agent._task_handler)
            results.append({'title': promotion.title, 'status': 'claimed_verified'})
            logger.success('CLAIMED_VERIFIED: {}', promotion.title)
        return results


class EpicAgent:
    def __init__(self, page, manual=False):
        self.page = page
        self.epic_games = EpicGames(page, manual=manual)

    async def collect_epic_games(self):
        await self.page.goto(URL_CLAIM, wait_until='domcontentloaded')
        if not await store_authenticated(self.page):
            raise AuthenticationError('Store session is not authenticated; collection aborted')
        promotions = await asyncio.to_thread(get_promotions)
        if not promotions:
            logger.info('NO_ACTIVE_PROMOTIONS: API returned no active zero-price promotions')
            return []
        return await self.epic_games.collect_weekly_games(promotions)
