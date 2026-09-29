import unittest

from tests.helpers import load_script_module


def load_module():
    return load_script_module('chiphell_fid26_monitor', 'chiphell_fid26_monitor.py')


class ChiphellMonitorTests(unittest.TestCase):
    def test_default_url_uses_dateline_author_sort(self):
        m = load_module()
        self.assertIn('fid=328', m.DEFAULT_URL)
        self.assertIn('filter=author', m.DEFAULT_URL)
        self.assertIn('orderby=dateline', m.DEFAULT_URL)

    def test_extract_thread_id_from_normalthread_row_id(self):
        m = load_module()
        self.assertEqual(m.extract_thread_id('normalthread_123456', ''), '123456')

    def test_extract_thread_id_from_discuz_thread_url(self):
        m = load_module()
        self.assertEqual(m.extract_thread_id('', 'https://www.chiphell.com/thread-987654-1-1.html'), '987654')
        self.assertEqual(m.extract_thread_id('', 'https://www.chiphell.com/forum.php?mod=viewthread&tid=24680'), '24680')

    def test_compact_excerpt_normalizes_and_truncates(self):
        m = load_module()
        text = ' 第一行\n\n\t第二行   第三行 ' + 'x' * 200
        out = m.compact_excerpt(text, limit=30)
        self.assertNotIn('\n', out)
        self.assertNotIn('\t', out)
        self.assertLessEqual(len(out), 31)
        self.assertTrue(out.endswith('…'))

    def test_discuz_jammer_html_is_removed_from_excerpt_source(self):
        m = load_module()
        html = '联想台式机拆机内存，200到付。<font class="jammer"># z& y: F8 F @2 M5</font><br><span style="display:none">$ hidden noise</span><ignore_js_op><img src="x.jpg"></ignore_js_op>补充一句。'
        out = m.clean_discuz_html(html)
        self.assertIn('联想台式机拆机内存，200到付。', out)
        self.assertIn('补充一句。', out)
        self.assertNotIn('jammer', out)
        self.assertNotIn('F8 F', out)
        self.assertNotIn('hidden noise', out)
        self.assertNotIn('img', out)

    def test_discuz_typeoption_fields_are_extracted_for_sale_posts(self):
        m = load_module()
        html = '''
        <div class="typeoption"><table summary="分类信息" class="cgtl mbm">
          <caption>在售</caption>
          <tr><th>姓名:</th><td>-</td></tr>
          <tr><th>所在地:</th><td>合肥 </td></tr>
          <tr><th>电话:</th><td>-</td></tr>
          <tr><th>QQ:</th><td>89477052 </td></tr>
          <tr><th>价格:</th><td>18500 元</td></tr>
          <tr><th>付款方式:</th><td>线下面交&nbsp;</td></tr>
          <tr><th>交易范围:</th><td>只出本地 </td></tr>
          <tr><th>成色:</th><td>全新 </td></tr>
        </table></div>
        '''
        out = m.parse_typeoption_html(html)
        self.assertIn('在售', out)
        self.assertIn('所在地：合肥', out)
        self.assertIn('QQ：89477052', out)
        self.assertIn('价格：18500 元', out)
        self.assertIn('成色：全新', out)
        self.assertNotIn('姓名', out)
        self.assertNotIn('电话', out)

    def test_notification_order_preserves_page_order(self):
        m = load_module()
        posts = [{'id': 'newest'}, {'id': 'middle'}, {'id': 'older'}]
        self.assertEqual([p['id'] for p in m.order_posts_for_notification(posts)], ['newest', 'middle', 'older'])

    def test_format_telegram_message_includes_body_excerpt_without_local_paths(self):
        m = load_module()
        posts = [
            {
                'id': '123',
                'title': '测试标题',
                'url': 'https://www.chiphell.com/thread-123-1-1.html',
                'body_excerpt': '这是首楼正文摘要，用于判断值不值得看。',
            }
        ]
        msg = m.format_telegram_message(posts, fid='26')
        self.assertIn('Chiphell 新帖', msg)
        self.assertNotIn('fid=26', msg)
        self.assertIn('测试标题', msg)
        self.assertIn('这是首楼正文摘要', msg)
        self.assertIn('thread-123-1-1.html', msg)
        self.assertIn('1. [测试标题](https://www.chiphell.com/thread-123-1-1.html) 摘要：这是首楼正文摘要，用于判断值不值得看。', msg)
        self.assertNotIn('\n\n1.', msg)
        self.assertNotIn('\n   摘要：', msg)
        self.assertNotIn('/srv/pylon/profiles/default', msg)
        self.assertNotIn('status', msg.lower())
    def test_should_notify_post_filters_excluded_device_deals(self):
        m = load_module()
        self.assertFalse(m.should_notify_post({'title': 'MacBook 官翻机清仓', 'body_excerpt': '明盘 9999'}))
        self.assertFalse(m.should_notify_post({'title': 'iPhone 有锁机低价出', 'body_excerpt': '明盘 1999'}))
        self.assertFalse(m.should_notify_post({'title': 'iPad 监管机便宜出', 'body_excerpt': '明盘 1888'}))
        self.assertTrue(m.should_notify_post({'title': 'iPhone 全新未拆封', 'body_excerpt': '无任何锁，非 MDM 设备，明盘 3999'}))


if __name__ == '__main__':
    unittest.main()
