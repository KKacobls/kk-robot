import unittest
from core import should_reply, safe_http_url, SearchResultParser, parse_gpu_csv, clean_text_for_discord

class CoreTest(unittest.TestCase):
    def test_empty_off(self):
        for mode in ('all', 'hybrid'):
            self.assertFalse(should_reply(mode, [], 1, None, False))
    def test_channel(self):
        self.assertTrue(should_reply('all', ['1'], 1, None, False))
        self.assertFalse(should_reply('all', ['2'], 1, None, True))
        self.assertTrue(should_reply('all', ['2'], 3, 2, False))
    def test_mentions(self):
        self.assertTrue(should_reply('hybrid', [], 1, None, True))
        self.assertTrue(should_reply('mention', [], 1, None, True))
        self.assertFalse(should_reply('mention', ['1'], 1, None, False))
        self.assertFalse(should_reply('invalid', ['1'], 1, None, False))
    def test_safe_url(self):
        self.assertIsNone(safe_http_url('http://127.0.0.1/test'))
        self.assertIsNone(safe_http_url('file:///etc/passwd'))
        self.assertIsNone(safe_http_url('http://localhost/'))
        self.assertEqual(safe_http_url('https://example.com/'), 'https://example.com/')
    def test_search(self):
        p = SearchResultParser()
        p.feed('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx">Example</a>')
        self.assertEqual(p.results[0]['url'], 'https://example.com/x')
    def test_gpu(self):
        self.assertEqual(parse_gpu_csv('NVIDIA L40S, 42, 19000, 49140')['utilization_percent'], 42)
        self.assertIsNone(parse_gpu_csv('N/A'))
    def test_output(self):
        self.assertNotIn('@everyone', ''.join(clean_text_for_discord('@everyone hello')))
if __name__ == '__main__': unittest.main()
