"""Exercise a controlling terminal while stdin is a pipe, like curl | bash."""
import errno
import os
import pty
import select
import signal
import sys
import time
import unittest
import updater

class TerminalTests(unittest.TestCase):
    def child(self, code, reply=None):
        pid, master = pty.fork()
        if pid == 0:
            os.execl(sys.executable, sys.executable, '-c', code)
        output = b''
        sent = False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output += chunk
                if reply and not sent and '隐藏输入'.encode() in output:
                    os.write(master, reply + b'\n')
                    sent = True
            else:
                self.fail('terminal subprocess timed out')
        finally:
            os.close(master)
            ended, status = os.waitpid(pid, os.WNOHANG)
            if not ended:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
        return output.decode(errors='replace'), status

    def test_original_text_read_write_mode_fails(self):
        out, _ = self.child("try:\n open('/dev/tty', 'r+')\nexcept OSError as e:\n print(type(e).__name__)\n")
        self.assertIn('UnsupportedOperation', out)

    def test_hidden_input_with_piped_stdin(self):
        code = """import os, updater
r,w=os.pipe()
os.close(w)
os.dup2(r,0)
os.close(r)
value=updater.prompt_bark_url()
print('INPUT_OK' if value == 'synthetic-bark-key' else 'INPUT_BAD')
"""
        out, _ = self.child(code, b'synthetic-bark-key')
        self.assertIn('INPUT_OK', out)
        self.assertNotIn('synthetic-bark-key', out)

    def test_no_tty_has_actionable_error(self):
        import subprocess
        result = subprocess.run([sys.executable, '-c', "import updater\ntry: updater.prompt_bark_url()\nexcept updater.Failure as e: print(e)"], stdin=subprocess.DEVNULL, capture_output=True, text=True, start_new_session=True, timeout=10)
        self.assertIn('--bark-url-file', result.stdout)

if __name__ == '__main__':
    unittest.main()
