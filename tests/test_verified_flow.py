import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import epic_authorization_service as auth
from services import epic_games_service as games


@pytest.fixture(autouse=True)
def no_real_challenges(monkeypatch):
    monkeypatch.setattr(auth, 'hcaptcha_visible', AsyncMock(return_value=False))
    monkeypatch.setattr(games, 'hcaptcha_visible', AsyncMock(return_value=False))


def offer():
    return {
        'id': 'offer-one', 'namespace': 'shared-namespace', 'title': 'A Collection',
        'price': {'totalPrice': {'originalPrice': 1000, 'discountPrice': 0}},
        'offerMappings': [{'pageSlug': 'a-game', 'pageType': 'productHome'}],
        'promotions': {'promotionalOffers': [{'promotionalOffers': [{
            'startDate': '2026-09-24T00:00:00Z', 'endDate': '2026-10-01T00:00:00Z',
            'discountSetting': {'discountPercentage': 0},
        }]}]},
    }


def parse(items):
    return games.parse_promotions({'data': {'Catalog': {'searchStore': {'elements': items}}}},
                                  datetime(2026, 9, 26, tzinfo=timezone.utc))


def test_active_offer_and_title_does_not_infer_bundle():
    result = parse([offer()])
    assert len(result) == 1
    assert result[0].url.endswith('/p/a-game')


def test_distinct_offers_in_same_namespace_are_kept():
    first, second = offer(), offer()
    second['id'] = 'offer-two'
    assert len(parse([first, second, first])) == 2


@pytest.mark.parametrize('case', ['future', 'expired', 'paid', 'permanent', 'no_promotions'])
def test_ineligible_offers_excluded(case):
    item = offer()
    promo = item['promotions']['promotionalOffers'][0]['promotionalOffers'][0]
    if case == 'future':
        promo['startDate'] = '2026-09-27T00:00:00Z'
    elif case == 'expired':
        promo['endDate'] = '2026-09-26T00:00:00Z'
    elif case == 'paid':
        item['price']['totalPrice']['discountPrice'] = 1
    elif case == 'permanent':
        item['price']['totalPrice']['originalPrice'] = 0
    else:
        item['promotions'] = None
    assert parse([item]) == []


def test_all_promotion_groups_are_checked():
    item = offer()
    item['promotions']['promotionalOffers'].insert(0, {'promotionalOffers': []})
    assert len(parse([item])) == 1


def test_malformed_api_is_not_reported_as_no_games():
    with pytest.raises(KeyError):
        games.parse_promotions({'error': 'unavailable'})


@pytest.mark.parametrize('text,expected', [
    ('$0.00', True), ('CNY 0.00', True), ('0,00 EUR', True), ('Free', True),
    ('$0.01', False), ('$10.00', False), ('Save $10.00', False),
    ('', False), ('0.00 10.00', False), ('Subtotal 0', False),
])
def test_checkout_amount(text, expected):
    assert games.zero_total(text) is expected


def test_anonymous_session_stops_collection(monkeypatch):
    page = SimpleNamespace(goto=AsyncMock())
    monkeypatch.setattr(games, 'store_authenticated', AsyncMock(return_value=False))
    get = AsyncMock()
    monkeypatch.setattr(games, 'get_promotions', get)
    with pytest.raises(auth.AuthenticationError):
        asyncio.run(games.EpicAgent(page).collect_epic_games())
    get.assert_not_called()


def test_collection_failure_propagates(monkeypatch):
    page = SimpleNamespace(goto=AsyncMock())
    monkeypatch.setattr(games, 'store_authenticated', AsyncMock(return_value=True))
    monkeypatch.setattr(games, 'get_promotions', lambda: parse([offer()]))
    agent = games.EpicAgent(page)
    agent.epic_games.collect_weekly_games = AsyncMock(side_effect=RuntimeError('not owned'))
    with pytest.raises(RuntimeError, match='not owned'):
        asyncio.run(agent.collect_epic_games())


def test_continue_is_valid_in_password_form(monkeypatch):
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             is_enabled=AsyncMock(return_value=True), click=AsyncMock())
    buttons = SimpleNamespace(all=AsyncMock(return_value=[button]))
    form = SimpleNamespace(count=AsyncMock(return_value=1), locator=lambda _: buttons)
    field = SimpleNamespace(locator=lambda _: form)
    monkeypatch.setattr(auth, 'interactive_challenge', AsyncMock(return_value=False))
    asyncio.run(auth.EpicAuthorization(SimpleNamespace())._submit(field))
    button.click.assert_awaited_once_with(timeout=5000)


def test_disabled_submit_never_force_clicked(monkeypatch):
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             is_enabled=AsyncMock(return_value=False), click=AsyncMock())
    buttons = SimpleNamespace(all=AsyncMock(return_value=[button]), count=AsyncMock(return_value=0))
    form = SimpleNamespace(count=AsyncMock(return_value=1), locator=lambda _: buttons)
    field = SimpleNamespace(locator=lambda _: form)
    monkeypatch.setattr(auth, 'interactive_challenge', AsyncMock(return_value=False))
    # Patch this module's clock, not the event loop's time module.
    monkeypatch.setattr(auth, 'time', SimpleNamespace(monotonic=iter([0, 1, 30]).__next__))
    with pytest.raises(auth.AuthenticationError, match='stayed disabled'):
        asyncio.run(auth.EpicAuthorization(SimpleNamespace())._submit(field))
    button.click.assert_not_awaited()


@pytest.mark.parametrize('total', ['$1.00', 'unavailable'])
def test_checkout_rejects_nonzero_or_missing_total(monkeypatch, total):
    button = SimpleNamespace(click=AsyncMock())
    text = f'A Collection\nTotal\n{total}'
    frame = SimpleNamespace(get_by_role=lambda *a, **k: object(),
                            locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value=text)))
    page = SimpleNamespace(frames=[frame])
    monkeypatch.setattr(games, 'interactive_challenge', AsyncMock(return_value=False))
    monkeypatch.setattr(games, 'first_visible', AsyncMock(return_value=button))
    with pytest.raises(RuntimeError, match='total'):
        asyncio.run(games.EpicGames(page)._checkout(parse([offer()])[0]))
    button.click.assert_not_awaited()


def test_wrong_checkout_product_rejected(monkeypatch):
    button = SimpleNamespace(click=AsyncMock())
    frame = SimpleNamespace(get_by_role=lambda *a, **k: object(), locator=lambda _: SimpleNamespace(
        inner_text=AsyncMock(return_value='Other game\nTotal\n$0.00')))
    monkeypatch.setattr(games, 'interactive_challenge', AsyncMock(return_value=False))
    monkeypatch.setattr(games, 'first_visible', AsyncMock(return_value=button))
    with pytest.raises(RuntimeError, match='title'):
        asyncio.run(games.EpicGames(SimpleNamespace(frames=[frame]))._checkout(parse([offer()])[0]))
    button.click.assert_not_awaited()


def test_buy_now_never_clicked():
    button = SimpleNamespace(inner_text=AsyncMock(return_value='Buy Now'), click=AsyncMock())
    agent = games.EpicGames(SimpleNamespace(goto=AsyncMock()))
    agent._cta = AsyncMock(return_value=button)
    with pytest.raises(RuntimeError, match='Unexpected CTA'):
        asyncio.run(agent.collect_weekly_games(parse([offer()])))
    button.click.assert_not_awaited()


@pytest.mark.parametrize('owned', [True, False])
def test_order_success_page_requires_ownership(monkeypatch, owned):
    button = SimpleNamespace(click=AsyncMock())
    frame = SimpleNamespace(get_by_role=lambda *a, **k: object(), locator=lambda _: SimpleNamespace(
        inner_text=AsyncMock(return_value='A Collection\nTotal\n$0.00')))
    page = SimpleNamespace(frames=[frame], url='https://store.epicgames.com/cart/success',
                           goto=AsyncMock(), get_by_text=lambda _: object())
    monkeypatch.setattr(games, 'interactive_challenge', AsyncMock(return_value=False))
    monkeypatch.setattr(games, 'first_visible', AsyncMock(return_value=button))
    monkeypatch.setattr(games, 'asyncio', SimpleNamespace(sleep=AsyncMock()))
    agent = games.EpicGames(page)
    agent._owned = AsyncMock(return_value=owned)
    if owned:
        asyncio.run(agent._checkout(parse([offer()])[0]))
    else:
        with pytest.raises(RuntimeError, match='unverified'):
            asyncio.run(agent._checkout(parse([offer()])[0]))
    button.click.assert_awaited_once()
