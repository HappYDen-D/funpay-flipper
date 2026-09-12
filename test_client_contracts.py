"""Network-mocked regression checks for marketplace mutation acknowledgements."""
import unittest
import os
from unittest.mock import AsyncMock, patch

import httpx

from auto_flipper.funpay_client import FunPayClient
from auto_flipper.funpay_transport import proxy_options, ProxyConfigurationError


class ClientContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FUNPAY_PROXY": ""}))
        self.client = FunPayClient("offline-test-key")
        self.client.action_authorizer = lambda action: True
        self.client.checkout_quote_validator = lambda page, lot: {"debit": 100, "currency": "RUB"}
        self.client.get_csrf_token = AsyncMock(return_value="test-csrf")
        self.transport = AsyncMock()
        self.transport.get.return_value = httpx.Response(200, text='<div class="chat" data-node="77"></div>')
        self.factory = self.enterContext(patch("auto_flipper.funpay_client.httpx.AsyncClient"))
        self.factory.return_value = self.transport
        self.transport.__aenter__.return_value = self.transport

    async def test_funpay_ignores_ambient_and_telegram_proxy_environment(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://ambient.invalid:8080",
                                    "HTTPS_PROXY": "http://ambient.invalid:8080",
                                    "ALL_PROXY": "socks5://ambient.invalid:1080",
                                    "TELEGRAM_PROXY": "http://telegram.invalid:8080"}):
            await self.client.get_account_info()
        options = self.factory.call_args.kwargs
        self.assertIs(options['trust_env'], False)
        self.assertIsNone(options['proxy'])

    async def test_every_funpay_http_operation_uses_one_explicit_proxy(self):
        self.client._proxy_url = 'http://test-user:test-password@proxy.invalid:8080'
        self.transport.post.return_value = httpx.Response(200, json={'error': 0, 'offer_id': '123'})
        # Token lookup and node resolution support caller-owned clients too;
        # direct calls must use the same configured transport as all mutations.
        self.client.get_csrf_token = FunPayClient.get_csrf_token.__get__(self.client)
        await self.client.get_csrf_token()
        self.transport.aclose.assert_awaited_once()
        self.client.get_csrf_token = AsyncMock(return_value='test-csrf')
        await self.client.get_account_info()
        await self.client.checkout_lot('123',100,dry_run=False)
        await self.client.save_offer(offer_id='123',price=100,dry_run=False)
        await self.client.resolve_chat_node_id('123')
        await self.client.send_chat_message('123','offline fixture',dry_run=False)
        await self.client.fetch_order_chat('123')
        await self.client.fetch_incoming_orders()
        await self.client.raise_lots([1350],dry_run=False)
        await self.client.fetch_market_lots([1350])
        self.assertEqual(self.factory.call_count, 10)
        for call in self.factory.call_args_list:
            self.assertEqual(call.kwargs['proxy'], self.client._proxy_url)
            self.assertIs(call.kwargs['trust_env'], False)

    def test_proxy_support_is_explicit_and_configuration_errors_are_redacted(self):
        for scheme in ('http','https','socks5','socks5h'):
            with self.subTest(scheme=scheme), patch('auto_flipper.funpay_transport.importlib.util.find_spec',return_value=object()):
                url = scheme + '://fixture:password@proxy.invalid:8080'
                self.assertEqual(proxy_options(url), {'proxy': url, 'trust_env': False})
        for url in ('ftp://fixture:password@proxy.invalid:21', 'http://fixture:password@:8080',
                    'http://fixture:password@proxy.invalid:99999', 'http://proxy.invalid/path'):
            with self.subTest(url=url), self.assertRaises(ProxyConfigurationError) as error:
                proxy_options(url)
            self.assertNotIn('password', str(error.exception))
        with patch('auto_flipper.funpay_transport.importlib.util.find_spec',return_value=None):
            with self.assertRaisesRegex(ProxyConfigurationError, 'SOCKS_DEPENDENCY_MISSING'):
                proxy_options('socks5://fixture:password@proxy.invalid:1080')

    async def test_invalid_proxy_does_not_fall_back_to_direct_connection(self):
        self.client._proxy_url = 'ftp://fixture:password@proxy.invalid:21'
        info = await self.client.get_account_info()
        self.assertFalse(info['is_authenticated'])
        self.assertIn('FUNPAY_PROXY_INVALID',info['error'])
        self.assertNotIn('password',info['error'])
        self.factory.assert_not_called()

    async def test_proxy_error_after_checkout_is_unknown_and_redacted_without_retry(self):
        self.client._proxy_url = 'http://fixture:secret-password@proxy.invalid:8080'
        self.transport.post.side_effect = httpx.ProxyError('proxy failed: ' + self.client._proxy_url)
        result = await self.client.checkout_lot('123',100,dry_run=False)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertIn('ProxyError',result['error'])
        self.assertNotIn('secret-password',str(result))
        self.assertNotIn('proxy.invalid',str(result))
        self.transport.post.assert_awaited_once()

    async def test_plain_403_is_not_automatically_called_a_cloudflare_challenge(self):
        self.transport.get.return_value = httpx.Response(403,text='Access denied')
        result = await self.client.get_account_info()
        self.assertEqual(result['session_status'],'error')
        self.assertEqual(result['error'],'HTTP_403_ACCESS_DENIED')

    async def test_challenge_before_checkout_blocks_payment_even_with_http_200(self):
        self.transport.get.return_value = httpx.Response(200,headers={'cf-mitigated':'challenge'},text='<html>challenge</html>')
        result = await self.client.checkout_lot('123',100,dry_run=False)
        self.assertEqual((result['status'],result['error']),('FAILED','CLOUDFLARE_CHALLENGE'))
        self.transport.post.assert_not_awaited()

    async def test_challenge_after_checkout_is_unknown_without_retry(self):
        self.transport.post.return_value = httpx.Response(403,headers={'cf-mitigated':'challenge'},text='<html>challenge</html>')
        result = await self.client.checkout_lot('123',100,dry_run=False)
        self.assertEqual((result['status'],result['error']),('UNKNOWN','CLOUDFLARE_CHALLENGE'))
        self.transport.post.assert_awaited_once()

    async def test_challenge_stops_category_scan_and_boost_dispatch(self):
        response = httpx.Response(200,headers={'cf-mitigated':'challenge'},text='<html>challenge</html>')
        self.transport.get.return_value = response
        self.assertEqual(await self.client.fetch_market_lots([1350,612]), [])
        self.transport.get.assert_awaited_once()
        self.transport.post.return_value = response
        result = await self.client.raise_lots([1350,612],dry_run=False)
        self.assertFalse(result['success'])
        self.transport.post.assert_awaited_once()

    async def checkout(self, response):
        self.transport.post.return_value = response
        return await self.client.checkout_lot("funpay_123", 100, dry_run=False)

    async def test_preflight_runs_after_fetch_and_blocks_post(self):
        def reject_stale():
            self.transport.get.assert_awaited_once()
            raise ValueError('stale financial state')
        result = await self.client.checkout_lot('funpay_123', 100, dry_run=False, preflight=reject_stale)
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'FAILED')
        self.transport.post.assert_not_awaited()

    async def test_matching_price_without_matching_sku_never_posts(self):
        result = await self.client.checkout_lot('funpay_123', 100, dry_run=False, expected_sku='TF2:725;6')
        self.assertEqual(result['error'], 'CHECKOUT_PRODUCT_CHANGED_OR_UNKNOWN')
        self.transport.post.assert_not_awaited()

    async def test_html_order_links_are_not_checkout_receipts(self):
        for status in (200, 500):
            with self.subTest(status=status):
                result = await self.checkout(httpx.Response(status, text='<a href="/orders/OLD123/">old order</a>'))
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "UNKNOWN")

    async def test_only_explicit_same_origin_checkout_acknowledgements_succeed(self):
        for response in (
            httpx.Response(200, json={"error": 0, "redirect": "/orders/NEW123/"}),
            httpx.Response(302, headers={"Location": "https://funpay.com/orders/NEW123/"}),
        ):
            with self.subTest(response=response):
                result = await self.checkout(response)
                self.assertTrue(result["success"])
                self.assertEqual(result["order_id"], "NEW123")

    async def test_checkout_rejects_error_status_missing_ack_and_foreign_redirect(self):
        for response in (
            httpx.Response(500, json={"error": 0, "redirect": "/orders/NEW123/"}),
            httpx.Response(200, json={"redirect": "/orders/NEW123/"}),
            httpx.Response(200, json={"error": 1, "redirect": "/orders/NEW123/"}),
            httpx.Response(200, json={"error": 0, "redirect": "https://other.test/orders/NEW123/"}),
            httpx.Response(302, headers={"Location": "https://other.test/orders/NEW123/"}),
        ):
            with self.subTest(response=response):
                result = await self.checkout(response)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "UNKNOWN")

    async def test_missing_quote_never_posts(self):
        self.client.checkout_quote_validator = None
        result = await self.checkout(httpx.Response(200, json={"error": 0}))
        self.assertEqual(result["error"], "CHECKOUT_QUOTE_UNVERIFIED")
        self.transport.post.assert_not_awaited()

    async def test_invalid_checkout_input_never_requests(self):
        for lot, price in (("SIM-123", 100), ("funpay_123", -1), ("funpay_123", float("nan")),
                           ("funpay_123", True), ("../orders/123", 100)):
            with self.subTest(lot=lot, price=price):
                result = await self.client.checkout_lot(lot, price, dry_run=False)
                self.assertFalse(result["success"])
        self.transport.get.assert_not_awaited()
        self.transport.post.assert_not_awaited()

    async def test_uncertain_checkout_does_not_retry(self):
        self.transport.post.side_effect = httpx.ReadTimeout("response lost")
        result = await self.client.checkout_lot("123", 100, dry_run=False)
        self.assertEqual(result["status"], "UNKNOWN")
        self.transport.post.assert_awaited_once()

    async def test_existing_offer_error_or_html_is_not_success(self):
        for response in (httpx.Response(200, json={"error": 1}), httpx.Response(200, text="login form"),
                         httpx.Response(302, headers={"Location": "/login/"})):
            with self.subTest(response=response):
                self.transport.post.return_value = response
                result = await self.client.save_offer(offer_id="123", price=100, dry_run=False)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "UNKNOWN")

    async def test_offer_ack_requires_real_created_id(self):
        self.transport.post.return_value = httpx.Response(200, json={"error": 0})
        result = await self.client.save_offer(price=100, dry_run=False)
        self.assertEqual(result["error"], "OFFER_ID_UNVERIFIED")
        self.transport.post.return_value = httpx.Response(200, json={"error": 0, "offer_id": 321})
        result = await self.client.save_offer(price=100, dry_run=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["offer_id"], "321")

    async def test_custom_fields_cannot_override_price_or_security_fields(self):
        for key in ("price", "csrf_token", "active", "offer_id", "amount"):
            with self.subTest(key=key):
                result = await self.client.save_offer(price=100, custom_fields={key: "0"}, dry_run=False)
                self.assertEqual(result["error"], "UNSAFE_CUSTOM_FIELD")
        self.transport.post.assert_not_awaited()

    async def test_numeric_order_id_is_resolved_instead_of_used_as_chat_node(self):
        node = await self.client.resolve_chat_node_id("123", client=self.transport)
        self.assertEqual(node, 77)
        self.transport.get.assert_awaited_once_with("https://funpay.com/orders/123/")

    async def test_synthetic_ids_never_enter_live_paths(self):
        self.assertIsNone(await self.client.resolve_chat_node_id("SIM-123", client=self.transport))
        self.assertEqual(await self.client.fetch_order_chat("SIM-123"), [])
        result = await self.client.send_chat_message("SIM-123", "test", dry_run=False)
        self.assertFalse(result["success"])
        result = await self.client.save_offer(offer_id="SIM-LOT-123", dry_run=False)
        self.assertFalse(result["success"])
        self.transport.get.assert_not_awaited()
        self.transport.post.assert_not_awaited()

    async def test_explicit_chat_node_must_match_order(self):
        result = await self.client.send_chat_message("123", "test", node_id=999, dry_run=False)
        self.assertFalse(result["success"])
        self.transport.post.assert_not_awaited()

    async def test_lost_delivery_response_is_unknown_without_retry(self):
        self.transport.post.side_effect = httpx.ReadTimeout("response lost")
        result = await self.client.send_chat_message("123", "test", dry_run=False)
        self.assertEqual(result["status"], "UNKNOWN")
        self.transport.post.assert_awaited_once()

    async def test_unpaid_and_negated_payment_statuses_are_not_delivered(self):
        statuses = ("paid", "unpaid", "не оплачен", "оплачен", "Оплачено", "not paid", "refunded")
        rows = [f'<a href="/orders/ID{i}/"><div class="tc-desc-text">item</div>'
                f'<div class="tc-user"><span class="media-user-name">buyer</span></div>'
                f'<div class="tc-status">{status}</div></a>' for i, status in enumerate(statuses)]
        self.transport.get.return_value = httpx.Response(200, text="".join(rows))
        orders = await self.client.fetch_incoming_orders()
        self.assertEqual([order["is_paid"] for order in orders], [True, False, False, True, True, False, False])

    async def test_missing_order_status_cannot_borrow_next_orders_paid_status(self):
        self.transport.get.return_value = httpx.Response(200, text=(
            '<a href="/orders/UNPAID1/"><div class="tc-desc-text">item one</div>'
            '<div class="tc-user"><span class="media-user-name">buyer one</span></div></a>'
            '<a href="/orders/PAID2/"><div class="tc-desc-text">item two</div>'
            '<div class="tc-user"><span class="media-user-name">buyer two</span></div>'
            '<div class="tc-status">paid</div></a>'))
        orders = await self.client.fetch_incoming_orders()
        self.assertEqual([order["order_id"] for order in orders], ["PAID2"])
        self.assertTrue(orders[0]["is_paid"])

    def test_conflicting_node_markers_are_not_guessed(self):
        self.assertIsNone(self.client.extract_chat_node_id(
            '<div data-node="1350"></div><div class="chat" data-node="77"></div>'))

    async def test_boost_html_is_not_success(self):
        self.transport.post.return_value = httpx.Response(200, text="login page")
        result = await self.client.raise_lots([1350], dry_run=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["raised_nodes"], [])

    async def test_missing_csrf_prevents_delivery_listing_and_boost(self):
        self.client.get_csrf_token.return_value = ""
        for result in (
            await self.client.send_chat_message("123", "test", dry_run=False),
            await self.client.save_offer(offer_id="123", dry_run=False),
            await self.client.raise_lots([1350], dry_run=False),
        ):
            self.assertEqual(result["error"], "CSRF_UNVERIFIED")
        self.transport.post.assert_not_awaited()

    def test_market_price_requires_explicit_rub_currency_on_same_element(self):
        def listing(unit):
            return ('<span class="currency-menu">RUB</span><a href="/lots/offer?id=123" class="tc-item">'
                    '<div class="tc-desc-text">Account full access</div>'
                    f'<div class="tc-price" data-s="123"><span class="unit">{unit}</span></div></a>')
        for currency in ("EUR", "€", "$", "", "BYN"):
            with self.subTest(currency=currency):
                self.assertEqual(self.client.parse_lots(listing(currency), node_id=1350), [])
        lots = self.client.parse_lots(listing("&#8381;"), node_id=1350)
        self.assertEqual(lots[0]["currency"], "RUB")
        self.assertEqual(lots[0]["price"], 123)


if __name__ == "__main__":
    unittest.main()
