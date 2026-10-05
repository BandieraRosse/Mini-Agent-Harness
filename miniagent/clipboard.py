"""Copy through native clipboard tools, or the terminal's OSC 52 protocol."""

import base64
import shutil
import subprocess
import sys


def copy_text(text, output):
    encoded = text.encode('utf-8')
    if sys.platform == 'win32':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   '$s=[Console]::In.ReadToEnd(); Set-Clipboard -Value '
                   '([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)))']
        encoded = base64.b64encode(encoded)
    elif sys.platform == 'darwin':
        command = ['pbcopy']
    else:
        command = next((args for args in (['wl-copy'], ['xclip', '-selection', 'clipboard'],
                                         ['xsel', '--clipboard', '--input'])
                        if shutil.which(args[0])), None)
    if command:
        try:
            subprocess.run(command, input=encoded, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2, check=True)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
    output.write_raw('\x1b]52;c;' + payload + '\x07')
    output.flush()
