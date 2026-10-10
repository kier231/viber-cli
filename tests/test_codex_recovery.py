"""Codex failures remain recoverable before any Viber input."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

from app.codex_replies import CodexReplies
from app.reply_errors import ReplyRetryable


class CodexRecoveryTests(unittest.TestCase):
    def generator(self):
        generator=CodexReplies(timeout=1)
        generator.check_login=Mock(return_value={'ready':True})
        generator.executable=Mock(return_value='codex')
        generator.preferences=Mock(return_value={})
        return generator

    def test_timeout_stops_old_generation_before_recovery(self):
        generator=self.generator()
        process=Mock(returncode=0)
        process.communicate.side_effect=[subprocess.TimeoutExpired('codex',1),('','')]
        with patch('app.codex_replies.subprocess.Popen',return_value=process):
            with self.assertRaises(ReplyRetryable) as error:
                generator.generate({'messages':[]},'Reply')
        self.assertEqual(error.exception.code,'codex_timeout')
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count,2)
        self.assertEqual(generator.processes,set())

    def test_invalid_structured_output_can_be_generated_again(self):
        generator=self.generator()
        def launch(command,**kwargs):
            Path(command[command.index('-o')+1]).write_text('broken json',encoding='utf-8')
            return Mock(returncode=0,communicate=lambda *_a,**_k:('',''))
        with patch('app.codex_replies.subprocess.Popen',side_effect=launch):
            with self.assertRaises(ReplyRetryable) as error:
                generator.generate({'messages':[]},'Reply')
        self.assertEqual(error.exception.code,'codex_invalid_output')

    def test_unexpected_tool_attempt_stays_held(self):
        generator=self.generator()
        event=json.dumps({'type':'item.completed','item':{'type':'command_execution'}})
        process=Mock(returncode=0,communicate=lambda *_a,**_k:(event,''))
        with patch('app.codex_replies.subprocess.Popen',return_value=process):
            with self.assertRaisesRegex(ValueError,'attempted a tool'):
                generator.generate({'messages':[]},'Reply')


if __name__=='__main__': unittest.main()
