import base64
import subprocess
import unittest
from unittest.mock import Mock, patch

from miniagent.clipboard import copy_text


class ClipboardTests(unittest.TestCase):
    def test_termux_copy_uses_utf8_stdin(self):
        output = Mock()
        text = '中文\n$HOME `command`'
        with patch('miniagent.clipboard.sys.platform', 'linux'), patch(
                'miniagent.clipboard.shutil.which', return_value='/termux/bin/tool'), patch(
                'miniagent.clipboard.subprocess.run') as run:
            copy_text(text, output)
        self.assertEqual(run.call_args.args[0], ['termux-clipboard-set'])
        self.assertEqual(run.call_args.kwargs['input'], text.encode('utf-8'))
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        output.write_raw.assert_not_called()

    def test_windows_clipboard_uses_utf8_base64_without_shell_interpolation(self):
        text = '中文\n$HOME `command`'
        with patch('miniagent.clipboard.sys.platform', 'win32'), patch('miniagent.clipboard.subprocess.run') as run:
            copy_text(text, Mock())
        self.assertEqual(base64.b64decode(run.call_args.kwargs['input']).decode('utf-8'), text)
        self.assertNotIn(text, ' '.join(run.call_args.args[0]))

    def test_failed_native_copy_falls_back_to_terminal_clipboard(self):
        output = Mock()
        with patch('miniagent.clipboard.sys.platform', 'darwin'), patch(
                'miniagent.clipboard.subprocess.run', side_effect=subprocess.TimeoutExpired('pbcopy', 2)):
            copy_text('中文\nanswer', output)
        data = output.write_raw.call_args.args[0]
        self.assertTrue(data.startswith('\x1b]52;c;'))
        self.assertTrue(data.endswith('\x07'))
        self.assertEqual(base64.b64decode(data[7:-1]).decode('utf-8'), '中文\nanswer')
        output.flush.assert_called_once()

    def test_failed_termux_copy_falls_back_to_osc52(self):
        output = Mock()
        with patch('miniagent.clipboard.sys.platform', 'linux'), patch(
                'miniagent.clipboard.shutil.which', return_value='/termux/bin/tool'), patch(
                'miniagent.clipboard.subprocess.run', side_effect=subprocess.CalledProcessError(
                    1, ['termux-clipboard-set'])):
            copy_text('中文', output)
        self.assertEqual(base64.b64decode(output.write_raw.call_args.args[0][7:-1]).decode('utf-8'), '中文')
