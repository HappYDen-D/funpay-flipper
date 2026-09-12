"""Small offline regression set for the Gemini engineering audit."""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

_bootstrap = tempfile.TemporaryDirectory(prefix='engineering-bootstrap-')
os.environ['FLIPPER_DB_PATH'] = str(Path(_bootstrap.name)/'bootstrap.db')
from auto_flipper.database import Database
from auto_flipper.economics import kopecks, stored_kopecks
from auto_flipper.flipper_engine import FlipperEngine
from auto_flipper.handlers import parse_goal_amount, format_boost_result, edit_text_if_changed, cb_browser_menu
from auto_flipper.bot import setup_bot_commands
from auto_flipper.funpay_transport import proxy_options, response_problem
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
import httpx


class EngineeringFixes(unittest.IsolatedAsyncioTestCase):
    def test_money_noise_is_tolerated_only_in_stored_float(self):
        self.assertEqual(stored_kopecks(349.9999999999),35000)
        for value in ('349.9999999999', Decimal('349.9999999999'), 349.999, True, float('nan')):
            with self.assertRaises(ValueError):
                stored_kopecks(value)
        with self.assertRaises(ValueError):
            kopecks(349.9999999999)

    def test_goal_whole_input_and_blocked_boost(self):
        self.assertEqual(parse_goal_amount('500 000'),Decimal('500000'))
        self.assertEqual(parse_goal_amount('1\u202f000,25'),Decimal('1000.25'))
        for value in ('NaN','500 abc','500 12','inf'):
            with self.assertRaises(ValueError):
                parse_goal_amount(value)
        output=format_boost_result({'success':False,'raise_info':{'success':False,'error':'MODE_BLOCKED'},'turbo_mode':False})
        self.assertNotIn('ВКЛЮЧЕН',output)

    async def test_menu_and_repeated_edit(self):
        bot=AsyncMock(); await setup_bot_commands(bot)
        commands={c.command for c in bot.set_my_commands.call_args.args[0]}
        self.assertTrue({'capital','prepare','asset_intake','manual_exit','settle','deadlines','alerts'} <= commands)
        message=AsyncMock()
        method=EditMessageText(chat_id=42,message_id=1,text='same')
        message.edit_text.side_effect=TelegramBadRequest(method=method,message='Bad Request: message is not modified')
        await edit_text_if_changed(message,'same')
        callback=AsyncMock(); callback.message=message
        with patch('auto_flipper.handlers.is_admin',return_value=True), patch(
                'auto_flipper.handlers.flipper_engine.client.get_account_info',
                new=AsyncMock(return_value={'session_status':'expired'})):
            await cb_browser_menu(callback)
        callback.answer.assert_awaited_once()
        message.edit_text.side_effect=TelegramBadRequest(method=method,message='Bad Request: chat not found')
        with self.assertRaises(TelegramBadRequest):
            await edit_text_if_changed(message,'same')

    def test_explicit_proxy_and_challenge_diagnosis(self):
        self.assertEqual(proxy_options(''),{'proxy':None,'trust_env':False})
        self.assertFalse(proxy_options('http://127.0.0.1:8080')['trust_env'])
        self.assertEqual(response_problem(httpx.Response(403,text='forbidden')),'HTTP_403_ACCESS_DENIED')
        self.assertEqual(response_problem(httpx.Response(200,headers={'cf-mitigated':'challenge'},text='challenge')),'CLOUDFLARE_CHALLENGE')

    async def test_scan_trade_items_and_alert_opt_in(self):
        with tempfile.TemporaryDirectory(prefix='engineering-scan-') as folder:
            db=Database(str(Path(folder)/'scan.db'))
            with patch('auto_flipper.flipper_engine.db',db), patch('auto_flipper.candidate_alerts.ADMIN_IDS',[42]):
                engine=FlipperEngine();engine.set_mode('OBSERVE');engine._bot=AsyncMock()
                payload=dict(lot_id='funpay_1',title='TF2 Ticket',price=70,currency='RUB',seller='seller',
                    seller_rating=5,seller_reviews=30,node_id=1808)
                engine.client.fetch_market_lots=AsyncMock(return_value=[payload])
                self.assertEqual(await engine.scan_and_autobuy_cycle(),[])
                self.assertIsNotNone(db.get_candidate('funpay_1'))
                self.assertEqual(db.get_inventory_list(),[])
                engine._bot.send_message.assert_not_awaited()
                db.set_setting('candidate_alerts','1')
                payload['price']=69
                await engine.scan_and_autobuy_cycle()
                await engine.scan_and_autobuy_cycle()
                engine._bot.send_message.assert_awaited_once()

    def test_load_dotenv_behavior(self):
        import auto_flipper.config as cfg
        with tempfile.TemporaryDirectory(prefix='dotenv-test-') as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text(
                "# Comment line\n"
                "TEST_VAR_A=hello_world\n"
                "TEST_VAR_B=\"quoted_string\"\n"
                "TEST_VAR_C='single_quoted'\n"
                "; Semicolon comment\n"
                "\n"
                "TEST_EXISTING=new_value\n",
                encoding="utf-8"
            )
            with patch.dict(os.environ, {"TEST_EXISTING": "original_value"}, clear=False):
                cfg.load_dotenv(env_file)
                self.assertEqual(os.environ.get("TEST_VAR_A"), "hello_world")
                self.assertEqual(os.environ.get("TEST_VAR_B"), "quoted_string")
                self.assertEqual(os.environ.get("TEST_VAR_C"), "single_quoted")
                self.assertEqual(os.environ.get("TEST_EXISTING"), "original_value")

            with patch.object(cfg, 'BASE_DIR', Path(folder)):
                with patch.dict(os.environ, {"FLIPPER_IGNORE_DOTENV": "true"}, clear=False):
                    os.environ.pop("TEST_VAR_A", None)
                    cfg.load_dotenv()
                    self.assertNotIn("TEST_VAR_A", os.environ)

            cfg.load_dotenv(Path(folder) / "non_existent.env")


if __name__ == '__main__':
    unittest.main()
