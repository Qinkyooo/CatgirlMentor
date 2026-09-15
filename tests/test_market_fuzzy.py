import json
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from nanobot.games.ffxiv.http import FetchResponse
from nanobot.games.ffxiv.market import MarketService
from nanobot.games.ffxiv.wiki import PINNED_SCHEMA, PINNED_VERSION, FFCafeClient


class ItemAndMarketHttp:
    """Offline fixture implementing the source's exact/substring query semantics."""

    def __init__(self, rows):
        self.rows = rows
        self.searches = []
        self.price_ids = []

    async def get_bytes(self, url, **kwargs):
        if '/search?' in url:
            query = parse_qs(urlsplit(url).query)['query'][0]
            self.searches.append(query)
            clauses = re.findall(r'Name([=~])"([^"]*)"', query)
            rows = [(i, name) for i, name in self.rows if any(
                name == term if op == '=' else term in name for op, term in clauses
            )]
            payload = {'schema': PINNED_SCHEMA, 'version': PINNED_VERSION,
                       'results': [{'row_id': i, 'fields': {'Name': name,
                                   'LevelItem': {'row_id': 30}}} for i, name in rows]}
        elif url.endswith('/marketable'):
            payload = [i for i, _ in self.rows]
        else:
            item_id = int(url.rsplit('/', 1)[-1])
            self.price_ids.append(item_id)
            payload = {'results': [{'itemId': item_id, 'nq': {
                'minListing': {'dc': {'price': 19000}},
                'averageSalePrice': {'dc': {'price': 20000}},
            }}]}
        return FetchResponse(url, 200, {}, json.dumps(payload).encode())


class MarketFuzzyTest(unittest.IsolatedAsyncioTestCase):
    async def query(self, name, rows):
        http = ItemAndMarketHttp(rows)
        service = MarketService(http=http, items=FFCafeClient(http=http))
        result = await service.execute(action='price', item_name=name, scope='陆行鸟')
        return json.loads(str(result)), http

    async def test_missing_words_resolve_to_real_item_and_disclose_name(self):
        result, http = await self.query('纯白隔离墙', [
            (17028, '白色隔离墙'), (24514, '纯白直板隔离墙'), (24999, '纯白礼服')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['item']['rowId'], 24514)
        self.assertEqual(http.price_ids, [24514])
        self.assertTrue(any('纯白隔离墙' in w and '纯白直板隔离墙' in w
                            for w in result['warnings']))

    async def test_exact_match_wins_without_fuzzy_request(self):
        result, http = await self.query('纯白直板隔离墙', [
            (24514, '纯白直板隔离墙'), (999, '纯白直板隔离墙改')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(http.price_ids, [24514])
        self.assertEqual(len(http.searches), 1)

    async def test_similar_candidates_require_choice(self):
        result, http = await self.query('纯白隔离墙', [
            (1, '纯白直板隔离墙'), (2, '纯白弧形隔离墙')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'ambiguous_item')
        self.assertEqual(len(result['suggestions']), 2)
        self.assertEqual(http.price_ids, [])

    async def test_short_category_does_not_auto_select(self):
        result, http = await self.query('隔离墙', [(24514, '纯白直板隔离墙')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'ambiguous_item')
        self.assertEqual(http.price_ids, [])

    async def test_unrelated_items_do_not_become_prices(self):
        result, http = await self.query('纯白隔离墙', [(24999, '纯白礼服')])
        self.assertFalse(result['ok'])
        self.assertEqual(http.price_ids, [])

    async def test_small_typo_matches_with_clear_lead(self):
        result, http = await self.query('纯白直板隔离强', [
            (24514, '纯白直板隔离墙'), (17028, '白色隔离墙')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(http.price_ids, [24514])


if __name__ == '__main__':
    unittest.main()
