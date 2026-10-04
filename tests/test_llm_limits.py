"""
Unit tests for the token-limit and failure-handling paths around the LLM calls.

Everything AWS is mocked: no request leaves the machine. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_context  # noqa: E402
import lambda_function as lf  # noqa: E402


def _body(payload):
    return {'body': io.BytesIO(json.dumps(payload).encode('utf-8'))}


def _claude(text, stop_reason='end_turn', thinking=True):
    content = [{'type': 'thinking', 'thinking': ''}] if thinking else []
    if text is not None:
        content.append({'type': 'text', 'text': text})
    return _body({'content': content, 'stop_reason': stop_reason})


def _nova(text, stop_reason='end_turn'):
    return _body({'output': {'message': {'content': [{'text': text}]}},
                  'stopReason': stop_reason})


def _sent_max_tokens(call):
    body = json.loads(call.kwargs['body'])
    if 'inferenceConfig' in body:
        return body['inferenceConfig']['maxTokens']
    return body['max_tokens']


class _BedrockCase(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        patcher = mock.patch.object(lf, '_bedrock_client', return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        lang = mock.patch.dict(lf.CONFIG, {'DIGEST_LANGUAGE': 'en'})
        lang.start()
        self.addCleanup(lang.stop)
        quiet = mock.patch('builtins.print')
        self.printed = quiet.start()
        self.addCleanup(quiet.stop)

    def logged(self):
        return '\n'.join(' '.join(str(a) for a in c.args) for c in self.printed.call_args_list)


class MaxTokensLimitTests(_BedrockCase):
    def test_lookup_table(self):
        self.assertEqual(lf._max_output_tokens('us.amazon.nova-pro-v1:0'), 10240)
        self.assertEqual(lf._max_output_tokens('us.amazon.nova-micro-v1:0'), 10240)
        self.assertEqual(lf._max_output_tokens('us.anthropic.claude-3-haiku-20240307-v1:0'), 4096)
        self.assertEqual(lf._max_output_tokens('us.anthropic.claude-opus-5-5'), 128000)
        self.assertIsNone(lf._max_output_tokens('some.unknown-model'))

    def test_nova_advice_call_is_clamped_to_10240(self):
        self.client.invoke_model.return_value = _nova('advice')
        with mock.patch.dict(lf.CONFIG, {'ADVICE_MODEL_ID': 'us.amazon.nova-pro-v1:0'}):
            self.assertEqual(lf._invoke_advice_llm('p'), 'advice')
        self.assertEqual(_sent_max_tokens(self.client.invoke_model.call_args), 10240)

    def test_claude_advice_call_keeps_16000(self):
        self.client.invoke_model.return_value = _claude('advice')
        with mock.patch.dict(lf.CONFIG, {'ADVICE_MODEL_ID': 'us.anthropic.claude-opus-5'}):
            lf._invoke_advice_llm('p')
        self.assertEqual(_sent_max_tokens(self.client.invoke_model.call_args), 16000)

    def test_claude_3_haiku_digest_is_clamped_to_4096(self):
        self.client.invoke_model.return_value = _claude('digest', thinking=False)
        lf._invoke_bedrock('p', model_id='us.anthropic.claude-3-haiku-20240307-v1:0')
        self.assertEqual(_sent_max_tokens(self.client.invoke_model.call_args), 4096)

    def test_unknown_model_is_not_clamped(self):
        self.client.invoke_model.return_value = _claude('digest')
        lf._invoke_bedrock('p', model_id='us.anthropic.claude-something-new', max_tokens=50000)
        self.assertEqual(_sent_max_tokens(self.client.invoke_model.call_args), 50000)


class StopReasonTests(_BedrockCase):
    MODEL = 'us.anthropic.claude-sonnet-5'

    def test_complete_response_is_one_call_and_unmarked(self):
        self.client.invoke_model.return_value = _claude('full digest')
        self.assertEqual(lf._invoke_bedrock('p', model_id=self.MODEL), 'full digest')
        self.assertEqual(self.client.invoke_model.call_count, 1)

    def test_truncated_response_is_retried_with_higher_ceiling(self):
        self.client.invoke_model.side_effect = [
            _claude('half a digest', 'max_tokens'),
            _claude('the whole digest'),
        ]
        out = lf._invoke_bedrock('p', model_id=self.MODEL)
        self.assertEqual(out, 'the whole digest')
        sent = [_sent_max_tokens(c) for c in self.client.invoke_model.call_args_list]
        self.assertEqual(sent, [lf.MAX_TOKENS, lf.RETRY_MAX_TOKENS])
        self.assertIn('retrying once', self.logged())

    def test_still_truncated_after_retry_is_marked_and_logged(self):
        self.client.invoke_model.side_effect = [
            _claude('part one', 'max_tokens'),
            _claude('part one and two', 'max_tokens'),
        ]
        out = lf._invoke_bedrock('p', model_id=self.MODEL)
        self.assertTrue(out.startswith('part one and two'))
        self.assertIn('this section was cut off', out)
        self.assertEqual(self.client.invoke_model.call_count, 2)
        self.assertIn('still cut off', self.logged())

    def test_notice_follows_digest_language(self):
        self.client.invoke_model.side_effect = [
            _claude('a', 'max_tokens'), _claude('ab', 'max_tokens'),
        ]
        with mock.patch.dict(lf.CONFIG, {'DIGEST_LANGUAGE': 'zh-TW'}):
            out = lf._invoke_bedrock('p', model_id=self.MODEL)
        self.assertIn('本段內容不完整', out)

    def test_empty_after_thinking_is_retried_then_raises(self):
        # The measured case: all 8,192 tokens spent thinking, no text block.
        self.client.invoke_model.side_effect = [
            _claude(None, 'max_tokens'),
            _claude(None, 'max_tokens'),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            lf._invoke_bedrock('p', model_id=self.MODEL)
        self.assertIn('no text', str(ctx.exception))
        self.assertEqual(self.client.invoke_model.call_count, 2)

    def test_empty_but_finished_raises_without_retry(self):
        self.client.invoke_model.return_value = _claude('   ')
        with self.assertRaises(RuntimeError):
            lf._invoke_bedrock('p', model_id=self.MODEL)
        self.assertEqual(self.client.invoke_model.call_count, 1)

    def test_nova_at_its_ceiling_is_marked_without_a_pointless_retry(self):
        self.client.invoke_model.return_value = _nova('partial advice', 'max_tokens')
        out = lf._invoke_bedrock('p', model_id='us.amazon.nova-pro-v1:0', max_tokens=16000)
        self.assertIn('this section was cut off', out)
        self.assertEqual(self.client.invoke_model.call_count, 1)

    def test_nova_digest_truncated_is_retried_up_to_its_ceiling(self):
        self.client.invoke_model.side_effect = [
            _nova('half', 'max_tokens'), _nova('whole'),
        ]
        out = lf._invoke_bedrock('p', model_id='us.amazon.nova-pro-v1:0')
        self.assertEqual(out, 'whole')
        sent = [_sent_max_tokens(c) for c in self.client.invoke_model.call_args_list]
        self.assertEqual(sent, [8192, 10240])


class OpenAICompatibleTests(unittest.TestCase):
    def _run(self, content, finish_reason):
        raw = json.dumps({'choices': [{'message': {'content': content},
                                       'finish_reason': finish_reason}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = raw
        cfg = {'LLM_BASE_URL': 'http://localhost:1234/v1', 'LLM_MODEL': 'm',
               'LLM_API_KEY_PARAM': '', 'DIGEST_LANGUAGE': 'en'}
        with mock.patch.dict(lf.CONFIG, cfg), \
             mock.patch.object(lf.urllib.request, 'urlopen', return_value=resp), \
             mock.patch('builtins.print'):
            return lf._invoke_openai_compatible('p')

    def test_length_finish_reason_is_marked(self):
        self.assertIn('this section was cut off', self._run('partial', 'length'))

    def test_stop_finish_reason_is_unmarked(self):
        self.assertEqual(self._run('full', 'stop'), 'full')

    def test_empty_content_raises(self):
        with self.assertRaises(RuntimeError):
            self._run('', 'length')


class AdviceSectionNeverRaisesTests(unittest.TestCase):
    SERVICES = [{'service': 'Amazon EC2', 'cost': 1.0, 'usage_types': []}]

    def setUp(self):
        patcher = mock.patch.object(account_context, '_collect',
                                    return_value=(self.SERVICES, [], ''))
        patcher.start()
        self.addCleanup(patcher.stop)
        fmt = mock.patch.object(account_context, 'format_account_services',
                                return_value=('listing', 0))
        fmt.start()
        self.addCleanup(fmt.stop)

    def test_model_error_becomes_a_warning(self):
        def reject(prompt):
            raise RuntimeError('ValidationException: max_tokens exceeds 10240')
        section, warn = account_context.build_advice_section('en', reject)
        self.assertEqual(section, '')
        self.assertIn('account advice skipped', warn)
        self.assertIn('ValidationException', warn)

    def test_success_still_returns_the_section(self):
        section, warn = account_context.build_advice_section('en', lambda p: 'do X')
        self.assertIn('do X', section)
        self.assertEqual(warn, '')

    def test_cost_explorer_error_is_still_a_warning(self):
        with mock.patch.object(account_context, '_collect', side_effect=RuntimeError('CE down')):
            section, warn = account_context.build_advice_section('en', lambda p: 'x')
        self.assertEqual(section, '')
        self.assertIn('CE down', warn)


class HandlerSendsDigestWhenAdviceFailsTests(unittest.TestCase):
    """End to end through lambda_handler: a rejected advice call must not turn
    the digest email into the error email."""

    def test_digest_is_sent_and_error_mail_is_not(self):
        features = dict(lf.CONFIG['FEATURES'], ACCOUNT_ADVICE=True, SAVE_TO_S3=False,
                        SEND_EMAIL=True, POST_TO_LINKEDIN=False, POST_TO_WEBHOOK=False)

        def advice_rejected(prompt):
            raise RuntimeError('ValidationException: maxTokens must be <= 10240')

        with mock.patch.dict(lf.CONFIG, {'FEATURES': features, 'DIGEST_LANGUAGE': 'en'}), \
             mock.patch.object(lf, 'fetch_aws_whats_new', return_value=[{'title': 't'}]), \
             mock.patch.object(lf, 'fetch_aws_blog_posts', return_value=[]), \
             mock.patch.object(lf, 'generate_digest', return_value='# digest'), \
             mock.patch.object(lf, '_invoke_advice_llm', side_effect=advice_rejected), \
             mock.patch.object(account_context, '_collect',
                               return_value=(AdviceSectionNeverRaisesTests.SERVICES, [], '')), \
             mock.patch.object(account_context, 'format_account_services',
                               return_value=('listing', 0)), \
             mock.patch.object(lf, 'send_email') as send_email, \
             mock.patch.object(lf, '_send_error_email') as send_error, \
             mock.patch('builtins.print') as printed:
            result = lf.lambda_handler({}, None)

        self.assertEqual(result['statusCode'], 200)
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[0], '# digest')
        send_error.assert_not_called()
        logged = '\n'.join(' '.join(map(str, c.args)) for c in printed.call_args_list)
        self.assertIn('WARNING: account advice skipped', logged)


if __name__ == '__main__':
    unittest.main()
