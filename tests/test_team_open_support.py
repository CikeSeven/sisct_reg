import sys
import unittest

sys.path.insert(0, 'backend')

from app.team_open_manager import TeamOpenManager
from app.team_open_store import parse_team_open_card_line, _split_team_open_card_import_records
from app.schemas import ImportTeamOpenCardsRequest
from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url


class TeamOpenSupportTests(unittest.TestCase):
    def test_parse_team_open_card_line_full_format(self):
        parsed = parse_team_open_card_line(
            '5349336394800665----04----32----007----lu----lu@gmail.com----US----CA----Santa Clarita----16442 Palomino Place----91387'
        )
        self.assertEqual('5349336394800665', parsed['card_number'])
        self.assertEqual('04', parsed['exp_month'])
        self.assertEqual('32', parsed['exp_year'])
        self.assertEqual('007', parsed['cvc'])
        self.assertEqual('lu', parsed['holder_name'])
        self.assertEqual('lu@gmail.com', parsed['billing_email'])
        self.assertEqual('US', parsed['country'])
        self.assertEqual('CA', parsed['state'])
        self.assertEqual('Santa Clarita', parsed['city'])
        self.assertEqual('16442 Palomino Place', parsed['line1'])
        self.assertEqual('91387', parsed['postal_code'])

    def test_parse_team_open_card_line_compact_format_uses_shared_address(self):
        parsed = parse_team_open_card_line(
            '5349336300545701 0431 881',
            default_holder_name='James Kvale',
            default_billing_email='lu@gmail.com',
            default_country='United States',
            default_state='CO',
            default_city='Almont',
            default_line1='120 Main Street',
            default_postal_code='81210',
        )
        self.assertEqual('5349336300545701', parsed['card_number'])
        self.assertEqual('04', parsed['exp_month'])
        self.assertEqual('31', parsed['exp_year'])
        self.assertEqual('881', parsed['cvc'])
        self.assertEqual('James Kvale', parsed['holder_name'])
        self.assertEqual('lu@gmail.com', parsed['billing_email'])
        self.assertEqual('US', parsed['country'])
        self.assertEqual('CO', parsed['state'])
        self.assertEqual('Almont', parsed['city'])
        self.assertEqual('120 Main Street', parsed['line1'])
        self.assertEqual('81210', parsed['postal_code'])

    def test_parse_team_open_card_line_mm_yy_format_uses_defaults(self):
        parsed = parse_team_open_card_line(
            '5349 3363 9480 0665----04/2032----007----US----CA----Santa Clarita----16442 Palomino Place----91387',
            default_holder_name='lu',
            default_billing_email='lu@gmail.com',
        )
        self.assertEqual('04', parsed['exp_month'])
        self.assertEqual('32', parsed['exp_year'])
        self.assertEqual('lu', parsed['holder_name'])
        self.assertEqual('lu@gmail.com', parsed['billing_email'])

    def test_normalize_team_open_options_applies_defaults_and_overrides(self):
        normalized = TeamOpenManager.normalize_options(
            {'team_open_payment_service_base_url': 'https://team.aimizy.com'},
            {
                'team_open_precheck_attempts': 3,
                'team_open_auto_submit_payment': False,
                'team_open_payment_service_country': 'sg',
                'team_open_payment_service_currency': 'sgd',
                'team_open_default_country': 'United States',
            },
        )
        self.assertEqual(3, normalized['team_open_precheck_attempts'])
        self.assertFalse(normalized['team_open_auto_submit_payment'])
        self.assertEqual('SG', normalized['team_open_payment_service_country'])
        self.assertEqual('SGD', normalized['team_open_payment_service_currency'])
        self.assertEqual('chatgptteamplan', normalized['team_open_payment_service_plan_name'])
        self.assertEqual('United States', normalized['team_open_default_country'])

    def test_parse_team_open_card_line_multiline_chinese_format(self):
        parsed = parse_team_open_card_line(
            '\n'.join([
                '卡号 5349336315877826',
                '有效期 0431',
                'CVV 605',
                '🕐开卡时间 2026/4/22 13:22:49',
                '剩余时间 {{COUNTDOWN:2026-04-22T14:22:57}}',
                '地区美国',
                '姓名 Kristian Hinson',
                '地址 230 Cliff Road',
                '城市 Lancaster',
                '州 SC',
                '邮编 29720',
                '国家 United States',
            ])
        )
        self.assertEqual('5349336315877826', parsed['card_number'])
        self.assertEqual('04', parsed['exp_month'])
        self.assertEqual('31', parsed['exp_year'])
        self.assertEqual('605', parsed['cvc'])
        self.assertEqual('Kristian Hinson', parsed['holder_name'])
        self.assertEqual('US', parsed['country'])
        self.assertEqual('SC', parsed['state'])
        self.assertEqual('Lancaster', parsed['city'])
        self.assertEqual('230 Cliff Road', parsed['line1'])
        self.assertEqual('29720', parsed['postal_code'])

    def test_split_team_open_card_import_records_supports_multiline_blocks(self):
        records = _split_team_open_card_import_records(
            '\n'.join([
                '卡号 5349336315877826',
                '有效期 0431',
                'CVV 605',
                '姓名 Kristian Hinson',
                '地址 230 Cliff Road',
                '城市 Lancaster',
                '州 SC',
                '邮编 29720',
                '国家 United States',
                '卡号 5349336315879999',
                '有效期 0532',
                'CVV 777',
            ])
        )
        self.assertEqual(2, len(records))
        self.assertEqual(1, records[0][0])
        self.assertIn('Kristian Hinson', records[0][1])
        self.assertEqual(10, records[1][0])
        self.assertIn('5349336315879999', records[1][1])
    def test_import_team_open_cards_request_coerces_numeric_default_postal_code(self):
        request = ImportTeamOpenCardsRequest(
            data='卡号 5349336315877826\n有效期 0431\nCVV 605',
            default_postal_code=95695,
        )
        self.assertEqual('95695', request.default_postal_code)

    def test_normalize_proxy_url_encodes_username_spaces(self):
        proxy = 'http://vphb1160539-region-US-st-South Carolina:ncmw9rdk@us.arxlabs.io:3010'
        normalized = normalize_proxy_url(proxy)
        self.assertEqual(
            'http://vphb1160539-region-US-st-South%20Carolina:ncmw9rdk@us.arxlabs.io:3010',
            normalized,
        )
        self.assertEqual(
            normalized,
            build_requests_proxy_config(proxy)['https'],
        )



if __name__ == '__main__':
    unittest.main()
